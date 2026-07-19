"""Phase 4 — semi-parametric marginal CDF per location (station x variable).

Empirical body + Generalized-Pareto (peaks-over-threshold) tails => a continuous, monotone,
invertible CDF that **extrapolates** beyond the sample (the non-negotiable for extremes).

  * continuous variables: empirical body between thresholds, GPD beyond, per config tails
    (temperature both, wind upper, pressure both, cloud body-only).
  * precipitation: a **censored / hurdle** marginal — point mass for dry (occurrence), an
    empirical+GPD wet body for intensity. Dry frequency is preserved exactly; sub-threshold
    simulated precip is hard-zeroed in the Phase-7 constraints.

Fit on standardized anomalies (stationary); seasonal modulation of extremes is restored later
via the climatology sigma(doy, lst).

# DECISION (D4.1): GPD fit + spliced CDF use scipy.stats.genpareto (full control over an exact
# invertible splice). pyextremes can be plugged for richer threshold diagnostics (MRL /
# parameter-stability) — a standalone MRL helper is provided here for acceptance.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.special import ndtri
from scipy.stats import genpareto

_EPS = 1e-6


def _emp_cdf_grid(samples: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    s = np.sort(samples)
    f = (np.arange(1, s.size + 1) - 0.5) / s.size      # Hazen plotting position
    return s, f


@dataclass
class SemiParametric:
    """Empirical body + optional GPD lower/upper tails. Invertible and monotone."""

    s: np.ndarray                      # sorted body samples
    f: np.ndarray                      # body CDF values
    f_lo: float | None = None          # lower-tail prob threshold (e.g. 0.05)
    u_lo: float = 0.0; c_lo: float = 0.0; sc_lo: float = 1.0
    f_up: float | None = None          # upper-tail prob threshold (e.g. 0.95)
    u_up: float = 0.0; c_up: float = 0.0; sc_up: float = 1.0

    def cdf(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype="float64")
        out = np.interp(x, self.s, self.f)             # body
        if self.f_up is not None:
            m = x > self.u_up
            out[m] = self.f_up + (1 - self.f_up) * genpareto.cdf(x[m] - self.u_up, self.c_up, 0, self.sc_up)
        if self.f_lo is not None:
            m = x < self.u_lo
            out[m] = self.f_lo * (1 - genpareto.cdf(self.u_lo - x[m], self.c_lo, 0, self.sc_lo))
        return np.clip(out, _EPS, 1 - _EPS)

    def ppf(self, u: np.ndarray) -> np.ndarray:
        u = np.clip(np.asarray(u, dtype="float64"), _EPS, 1 - _EPS)
        out = np.interp(u, self.f, self.s)             # body
        if self.f_up is not None:
            m = u > self.f_up
            out[m] = self.u_up + genpareto.ppf((u[m] - self.f_up) / (1 - self.f_up), self.c_up, 0, self.sc_up)
        if self.f_lo is not None:
            m = u < self.f_lo
            out[m] = self.u_lo - genpareto.ppf(1 - u[m] / self.f_lo, self.c_lo, 0, self.sc_lo)
        return out


@dataclass
class Censored:
    """Hurdle marginal: point mass p_dry for dry, SemiParametric for wet intensity."""

    p_dry: float
    dry_q: np.ndarray                  # sorted dry-side anomaly values (for inverse)
    wet: SemiParametric

    def cdf(self, x: np.ndarray, dry: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype="float64")
        out = np.empty_like(x)
        d = dry.astype(bool)
        out[d] = np.clip(np.interp(x[d], self.dry_q, np.linspace(0, self.p_dry, self.dry_q.size)), 0, self.p_dry)
        out[~d] = self.p_dry + (1 - self.p_dry) * self.wet.cdf(x[~d])
        return np.clip(out, _EPS, 1 - _EPS)

    # deep-negative anomaly for dry hours: after climatology reconstruct + expm1 it maps to an
    # exact 0 (so light wet precip is preserved instead of being hard-zeroed at a threshold).
    DRY_SENTINEL = -30.0

    def ppf(self, u: np.ndarray) -> np.ndarray:
        u = np.clip(np.asarray(u, dtype="float64"), _EPS, 1 - _EPS)
        out = np.empty_like(u)
        d = u < self.p_dry
        out[d] = self.DRY_SENTINEL
        out[~d] = self.wet.ppf((u[~d] - self.p_dry) / (1 - self.p_dry))
        return out


def _fit_gpd(sample: np.ndarray, q: float, upper: bool) -> tuple:
    """Fit a GPD to exceedances (upper) or deficits (lower) past the q-quantile threshold."""
    thr = np.quantile(sample, q if upper else 1 - q)
    exc = (sample[sample > thr] - thr) if upper else (thr - sample[sample < thr])
    exc = exc[exc > 0]
    if exc.size < 30:
        return None
    c, _, sc = genpareto.fit(exc, floc=0)
    c = float(np.clip(c, -0.5, 0.4))     # bound the shape: avoid runaway/infinite-variance tails
    return thr, c, float(sc)


def _fit_semiparametric(z: np.ndarray, tails: list[str], q: float) -> SemiParametric:
    z = z[~np.isnan(z)]
    s, f = _emp_cdf_grid(z)
    m = SemiParametric(s=s, f=f)
    if "upper" in tails:
        r = _fit_gpd(z, q, upper=True)
        if r:
            m.u_up, m.c_up, m.sc_up = r
            m.f_up = q
    if "lower" in tails:
        r = _fit_gpd(z, q, upper=False)
        if r:
            m.u_lo, m.c_lo, m.sc_lo = r
            m.f_lo = 1 - q
    return m


@dataclass
class MarginalSet:
    cols: list = field(default_factory=list)     # per-column SemiParametric | Censored | None

    def to_uniform(self, mat: np.ndarray, dry: np.ndarray | None = None) -> np.ndarray:
        u = np.full_like(mat, np.nan, dtype="float64")
        for j, m in enumerate(self.cols):
            if m is None:
                continue
            col = mat[:, j]
            ok = ~np.isnan(col)
            if isinstance(m, Censored):
                u[ok, j] = m.cdf(col[ok], dry[ok, j])
            else:
                u[ok, j] = m.cdf(col[ok])
        return u

    def from_uniform(self, u: np.ndarray) -> np.ndarray:
        x = np.empty_like(u, dtype="float64")
        for j, m in enumerate(self.cols):
            if m is None:
                # degenerate column (too little data to fit a marginal): standard-normal
                # anomaly fallback so the column is never NaN (improves once ERA5 infills it)
                x[:, j] = ndtri(np.clip(u[:, j], _EPS, 1 - _EPS))
            else:
                x[:, j] = m.ppf(u[:, j])
        return x


def fit(mat: np.ndarray, keys: list, variables_cfg: dict, q: float,
        dry: np.ndarray | None = None) -> MarginalSet:
    cols = []
    for j, (_station, var) in enumerate(keys):
        vcfg = variables_cfg.get(var, {})
        z = mat[:, j]
        if z[~np.isnan(z)].size < 100:
            cols.append(None)
            continue
        if vcfg.get("kind") == "intermittent" and dry is not None:
            d = dry[:, j].astype(bool) & ~np.isnan(z)
            p_dry = float(d.sum() / np.isfinite(z).sum())
            dry_q = np.sort(z[d]) if d.any() else np.array([np.nanmin(z)])
            wet = _fit_semiparametric(z[~d & ~np.isnan(z)], vcfg.get("tails", ["upper"]), q)
            cols.append(Censored(p_dry=p_dry, dry_q=dry_q, wet=wet))
        else:
            cols.append(_fit_semiparametric(z, vcfg.get("tails", []), q))
    return MarginalSet(cols=cols)


# --------------------------------------------------------------------------- #
#  Threshold diagnostic (acceptance): mean residual life
# --------------------------------------------------------------------------- #
def mean_residual_life(z: np.ndarray, quantiles: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    z = z[~np.isnan(z)]
    qs = quantiles if quantiles is not None else np.linspace(0.80, 0.99, 20)
    thr = np.quantile(z, qs)
    mrl = np.array([(z[z > t] - t).mean() if (z > t).sum() > 5 else np.nan for t in thr])
    return thr, mrl
