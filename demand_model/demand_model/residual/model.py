"""DM Phase 4 — stochastic residual model.

The statistical core (Phase 3) predicts the *conditional mean* load
``m(t) = f_base + f_heat + f_cool + f_light``. The true load is
``load(t) = m(t) + ε(t)`` where ε is short-term, weather/calendar-unexplained
variation: highly autocorrelated hour-to-hour and heteroscedastic (bigger in
cold winter evenings than mild summer nights). Extrapolating the mean alone would
produce implausibly smooth demand and understate peak/tail risk in the price step.

This layer models ε as a **heteroscedastic seasonal-hourly AR process**:

    z(t) = ε(t) / σ(bucket(t))                    # standardise out the seasonal-hourly scale
    z(t) = φ₁·z(t-1) + φ₂·z(t-2) + η(t)           # AR(2) on the standardised residual
    ε_sim(t) = σ(bucket(t)) · z_sim(t)

* ``σ`` is estimated per **(season × hour × weekend)** bucket (192 cells, each with
  hundreds of samples over 2015-2026) — this is the heteroscedasticity.
* the AR captures the persistence (residual lag-1 autocorrelation ≈ 0.9).
* innovations ``η`` are **bootstrapped** from the empirical AR-innovation pool by
  default (preserves the non-Gaussian, fat-tailed shape of demand shocks); a
  Gaussian option is available. Everything is **seeded / reproducible**.

Buckets are derived from the timestamp alone (Europe/Paris local time), so
``simulate`` needs only a future hourly ``DatetimeIndex`` — no feature frame.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from powersim_core.serialize import load_params, save_params

_SEASON = {12: "DJF", 1: "DJF", 2: "DJF", 3: "MAM", 4: "MAM", 5: "MAM",
           6: "JJA", 7: "JJA", 8: "JJA", 9: "SON", 10: "SON", 11: "SON"}


def _buckets(index: pd.DatetimeIndex) -> pd.Index:
    """(season × local-hour × weekend) key per timestamp — the heteroscedasticity cells."""
    if index.tz is None:
        index = index.tz_localize("UTC")
    local = index.tz_convert("Europe/Paris")
    season = local.month.map(_SEASON)
    weekend = (local.dayofweek >= 5).astype(int)
    keys = [f"{s}|{h:02d}|{w}" for s, h, w in zip(season, local.hour, weekend)]
    return pd.Index(keys, name="bucket")


def _fit_ar(z: pd.Series, order: int) -> tuple[np.ndarray, np.ndarray]:
    """OLS AR(order) on a (possibly gappy) hourly standardised series → (phi, innovation pool).

    Built on the full hourly grid so lags respect real time steps; rows with any missing
    lag are dropped. No intercept (z is ~zero-mean). AR coefficients are shrunk toward 0 if
    the process is non-stationary (companion spectral radius ≥ 0.999)."""
    grid = z.asfreq("h")                                     # regular hourly axis, NaN in gaps
    lags = pd.concat({k: grid.shift(k) for k in range(order + 1)}, axis=1)
    lags.columns = list(range(order + 1))
    ok = lags.notna().all(axis=1)
    Y = lags.loc[ok, 0].to_numpy()
    Xl = lags.loc[ok, list(range(1, order + 1))].to_numpy()
    phi, *_ = np.linalg.lstsq(Xl, Y, rcond=None)
    phi = _stabilise(phi)
    innov = Y - Xl @ phi
    return phi, innov


def _stabilise(phi: np.ndarray) -> np.ndarray:
    """Shrink AR coefficients until the companion matrix is inside the unit circle."""
    p = len(phi)
    for _ in range(50):
        comp = np.zeros((p, p))
        comp[0] = phi
        if p > 1:
            comp[1:, :-1] = np.eye(p - 1)
        if np.max(np.abs(np.linalg.eigvals(comp))) < 0.999:
            return phi
        phi = phi * 0.95
    return phi


@dataclass
class ResidualModel:
    order: int
    phi: np.ndarray                     # AR coefficients, length = order
    sigma: pd.Series                    # index = bucket key -> residual std (MW)
    sigma_global: float                 # fallback for unseen/thin buckets
    innovations: np.ndarray             # empirical AR-innovation pool (standardised)
    innov_std: float
    metrics: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ sampling
    def _sigma_vec(self, index: pd.DatetimeIndex) -> np.ndarray:
        keys = _buckets(index)
        s = self.sigma.reindex(keys).to_numpy()
        s[np.isnan(s)] = self.sigma_global
        return s

    def simulate(self, index: pd.DatetimeIndex, n_paths: int = 1, seed: int | None = None,
                 innovations: str = "bootstrap", burn_in: int = 256,
                 rng: np.random.Generator | None = None) -> pd.DataFrame:
        """Draw ``n_paths`` reproducible residual paths (MW) on a contiguous hourly ``index``.

        ``innovations`` = 'bootstrap' (resample empirical η, fat-tailed) or 'gaussian'.
        Returns a DataFrame index×['path_000',...]. Assumes ``index`` is regular hourly. Pass ``rng``
        (the F4 single-authority generator); ``seed`` is the legacy fallback."""
        rng = rng if rng is not None else np.random.default_rng(seed)
        n = len(index)
        p = self.order
        sig = self._sigma_vec(index)
        out = np.empty((n, n_paths))
        for j in range(n_paths):
            if innovations == "gaussian":
                eta = rng.normal(0.0, self.innov_std, n + burn_in)
            else:
                eta = rng.choice(self.innovations, size=n + burn_in, replace=True)
            z = np.zeros(n + burn_in)
            for t in range(p, n + burn_in):
                z[t] = self.phi @ z[t - p:t][::-1] + eta[t]
            out[:, j] = z[burn_in:] * sig
        cols = [f"path_{j:03d}" for j in range(n_paths)]
        return pd.DataFrame(out, index=index, columns=cols)

    # ------------------------------------------------------------------ io
    def save(self, path: str | Path) -> Path:
        """Portable JSON + npz (no pickle — REVIEW F6); `phi`/`innovations` arrays go to the sidecar,
        the `sigma` Series is stored as index + values."""
        payload = asdict(self)
        payload["sigma"] = {"index": list(self.sigma.index), "values": self.sigma.to_numpy(),
                            "name": self.sigma.name}
        return save_params(payload, Path(path).with_suffix(".json"))

    @staticmethod
    def load(path: str | Path) -> ResidualModel:
        d = load_params(Path(path).with_suffix(".json"))
        s = d["sigma"]
        d["sigma"] = pd.Series(s["values"], index=s["index"], name=s["name"])
        return ResidualModel(**d)


def fit_residual_model(resid: pd.Series, order: int = 2, min_count: int = 50,
                       seed: int | None = None) -> ResidualModel:
    """Fit the heteroscedastic AR residual model from a mean-model residual series (MW).

    ``resid`` is indexed by tz-aware UTC hourly timestamps (NaNs allowed / will be dropped)."""
    resid = resid.dropna().sort_index()
    keys = _buckets(resid.index)
    g = resid.groupby(keys)
    sigma = g.std(ddof=1)
    counts = g.count()
    sigma_global = float(resid.std(ddof=1))
    sigma = sigma.where(counts >= min_count, sigma_global)   # guard thin cells
    sigma = sigma.fillna(sigma_global)

    z = resid / sigma.reindex(keys).to_numpy()               # standardise
    phi, innov = _fit_ar(pd.Series(z.to_numpy(), index=resid.index), order)

    # diagnostics: empirical vs modelled lag-1 autocorrelation + a seeded self-check
    z_grid = pd.Series(z.to_numpy(), index=resid.index).asfreq("h")
    acf1 = float(z_grid.corr(z_grid.shift(1)))
    model = ResidualModel(order=order, phi=phi, sigma=sigma, sigma_global=sigma_global,
                          innovations=innov, innov_std=float(innov.std(ddof=1)))
    sim = model.simulate(resid.index, n_paths=1, seed=seed)["path_000"]
    model.metrics = {
        "n": int(resid.size), "n_buckets": int((counts >= min_count).sum()),
        "resid_std_mw": round(sigma_global, 1),
        "sigma_min_mw": round(float(sigma.min()), 1), "sigma_max_mw": round(float(sigma.max()), 1),
        "phi": [round(float(x), 4) for x in phi], "innov_std": round(float(innov.std(ddof=1)), 4),
        "acf1_empirical": round(acf1, 4),
        "acf1_simulated": round(float(sim.corr(sim.shift(1))), 4),
        "std_ratio_sim_over_emp": round(float(sim.std() / resid.std()), 4),
    }
    return model
