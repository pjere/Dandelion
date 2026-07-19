"""Phase 2 — climatology decomposition: x = mu(doy, lst) + sigma(doy, lst) * z.

Harmonic regression on BOTH the mean and the log-variance, using seasonal (annual +
semi-annual + ...) harmonics interacted with diurnal harmonics, so the diurnal amplitude
can vary by season. Hours are in **local solar time** (per-station longitude offset).

Heteroscedasticity is modelled explicitly: log sigma^2(doy, lst) shares the same basis.
The standardized anomaly z = (x - mu)/sigma is what the downstream phases consume.

# DECISION (D0.2): solar is excluded from the variable set, so there is no raw-GHI
# deseasonalization hazard here. All configured variables share this harmonic treatment;
# for the intermittent variable (precip) the anomaly is not Gaussian — that is handled by
# the hurdle/censored marginal in Phases 3-4, downstream of this standardization.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import xarray as xr

# E[log chi^2_1] = psi(1/2) + log 2 = -1.2704 ; correct OLS-on-log(r^2) back to log sigma^2
_LOGCHI2_BIAS = 1.2703628454614782


@dataclass
class HarmonicSpec:
    seasonal: int = 3
    diurnal: int = 3
    interact: bool = True
    use_lst: bool = True

    @property
    def n_basis(self) -> int:
        s, d = 2 * self.seasonal, 2 * self.diurnal
        return 1 + s + d + (s * d if self.interact else 0)


def _features(time: pd.DatetimeIndex, offset_h: float, spec: HarmonicSpec) -> np.ndarray:
    """Build the (T, n_basis) harmonic design matrix for one station (its LST offset)."""
    doy = (time.dayofyear.to_numpy() - 1 + time.hour.to_numpy() / 24.0) / 365.25
    hour = time.hour.to_numpy().astype("float64")
    lst = (hour + (offset_h if spec.use_lst else 0.0)) % 24.0

    seas = []
    for k in range(1, spec.seasonal + 1):
        seas += [np.sin(2 * np.pi * k * doy), np.cos(2 * np.pi * k * doy)]
    diur = []
    for j in range(1, spec.diurnal + 1):
        diur += [np.sin(2 * np.pi * j * lst / 24.0), np.cos(2 * np.pi * j * lst / 24.0)]

    cols = [np.ones(len(time))] + seas + diur
    if spec.interact:
        for s in seas:
            for d in diur:
                cols.append(s * d)
    return np.column_stack(cols)


@dataclass
class Climatology:
    spec: HarmonicSpec
    station_ids: list[str]
    var_names: list[str]
    offsets: dict[str, float]              # station_id -> LST offset (h)
    beta_mean: np.ndarray                  # (S, V, n_basis)
    beta_var: np.ndarray                   # (S, V, n_basis)

    # ----- per-station mu / sigma --------------------------------------------
    def _mu_sigma(self, time: pd.DatetimeIndex, si: int) -> tuple[np.ndarray, np.ndarray]:
        design = _features(time, self.offsets[self.station_ids[si]], self.spec)
        mu = design @ self.beta_mean[si].T            # (T, V)
        log_var = design @ self.beta_var[si].T + _LOGCHI2_BIAS
        sigma = np.exp(0.5 * np.clip(log_var, -30, 30))
        return mu, sigma

    def standardize(self, cube: xr.DataArray) -> xr.DataArray:
        """x -> z = (x - mu) / sigma."""
        out = cube.copy()
        time = pd.DatetimeIndex(cube["time"].values)
        for si in range(cube.sizes["station"]):
            mu, sigma = self._mu_sigma(time, si)
            out.values[:, si, :] = (cube.values[:, si, :] - mu) / sigma
        return out

    def reconstruct(self, anom: xr.DataArray) -> xr.DataArray:
        """z -> x = mu + sigma * z (rebuilds the climatology on the anomaly's own time axis)."""
        out = anom.copy()
        time = pd.DatetimeIndex(anom["time"].values)
        ids = [str(s) for s in anom["station"].values]
        for si, sid in enumerate(ids):
            k = self.station_ids.index(sid)
            design = _features(time, self.offsets[sid], self.spec)
            mu = design @ self.beta_mean[k].T
            log_var = design @ self.beta_var[k].T + _LOGCHI2_BIAS
            sigma = np.exp(0.5 * np.clip(log_var, -30, 30))
            out.values[:, si, :] = mu + sigma * anom.values[:, si, :]
        return out


def fit(cube: xr.DataArray, spec: HarmonicSpec) -> Climatology:
    time = pd.DatetimeIndex(cube["time"].values)
    station_ids = [str(s) for s in cube["station"].values]
    var_names = [str(v) for v in cube["variable"].values]
    offsets = {sid: float(o) for sid, o in zip(station_ids, cube["lst_offset_h"].values)}
    S, V, B = len(station_ids), len(var_names), spec.n_basis
    beta_mean = np.zeros((S, V, B))
    beta_var = np.zeros((S, V, B))
    fitted = np.zeros((S, V), dtype=bool)

    for si in range(S):
        design = _features(time, offsets[station_ids[si]], spec)
        for vi in range(V):
            x = cube.values[:, si, vi]
            ok = ~np.isnan(x)
            if ok.sum() < 2 * B:
                continue
            bm, *_ = np.linalg.lstsq(design[ok], x[ok], rcond=None)
            beta_mean[si, vi] = bm
            r = x[ok] - design[ok] @ bm
            bv, *_ = np.linalg.lstsq(design[ok], np.log(r**2 + 1e-6), rcond=None)
            beta_var[si, vi] = bv
            fitted[si, vi] = True

    # degenerate columns (too little data, e.g. a sparse station): borrow the cross-station
    # mean climatology for that variable so they produce PHYSICAL values (not mu=0). This is
    # what the ERA5 gap-infill will supersede once the extended record is folded in.
    for vi in range(V):
        good = fitted[:, vi]
        if good.any() and not good.all():
            beta_mean[~good, vi] = beta_mean[good, vi].mean(axis=0)
            beta_var[~good, vi] = beta_var[good, vi].mean(axis=0)

    return Climatology(spec, station_ids, var_names, offsets, beta_mean, beta_var)
