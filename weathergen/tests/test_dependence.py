"""Phase 5 unit test: the simulated latent reproduces cross-sectional correlation + ACF, and now
the SEASONAL (month-varying) cross-variable correlation (D5.3)."""
from __future__ import annotations

import numpy as np

from weathergen import dependence


def _make_latent(T: int, rng: np.random.Generator) -> np.ndarray:
    """6-dim standard-normal field: AR(1) common factor + idiosyncratic noise.
    corr(i,j) = load_i*load_j ; lag-1 autocorr inherited from the factor (phi=0.8)."""
    phi = 0.8
    f = np.empty(T)
    f[0] = rng.standard_normal()
    for t in range(1, T):
        f[t] = phi * f[t - 1] + np.sqrt(1 - phi**2) * rng.standard_normal()
    load = np.array([0.9, 0.8, 0.7, 0.6, 0.5, 0.4])
    noise = rng.standard_normal((T, load.size)) * np.sqrt(1 - load**2)
    return f[:, None] * load[None, :] + noise


def _months(T: int) -> np.ndarray:
    return (np.arange(T) // 730) % 12 + 1        # ~monthly blocks


def _lag1(x: np.ndarray) -> np.ndarray:
    return np.array([np.corrcoef(x[:-1, j], x[1:, j])[0, 1] for j in range(x.shape[1])])


def test_dependence_reproduces_correlation_and_acf():
    rng = np.random.default_rng(0)
    Y = _make_latent(20000, rng)
    months = _months(len(Y))
    dep = dependence.fit(Y, months, eof_variance=0.95, var_order=2, copula="gaussian", rng=rng)
    sim = dep.simulate(months, np.random.default_rng(1))

    c_obs, c_sim = np.corrcoef(Y, rowvar=False), np.corrcoef(sim, rowvar=False)
    off = ~np.eye(6, dtype=bool)
    assert np.mean(np.abs(c_obs[off] - c_sim[off])) < 0.08          # cross-sectional corr
    assert np.max(np.abs(_lag1(Y) - _lag1(sim))) < 0.1             # lag-1 ACF
    assert np.all(np.abs(sim.std(axis=0) - 1.0) < 0.15)           # ~unit variance per dim


def test_seasonal_cross_correlation_is_reproduced():
    """Two variables correlated ONLY in 'winter' (months 12,1,2) — a stationary model would wash it
    out; the month-varying innovation covariance must reproduce it per season."""
    rng = np.random.default_rng(2)
    T = 24 * 365 * 6
    months = (np.arange(T) // 24 % 365)
    month_of = np.searchsorted(np.cumsum([31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]), months % 365, "right") + 1
    winter = np.isin(month_of, [12, 1, 2])
    a = rng.standard_normal(T)
    b = np.where(winter, 0.8 * a + np.sqrt(1 - 0.64) * rng.standard_normal(T), rng.standard_normal(T))
    Y = np.column_stack([a, b])
    dep = dependence.fit(Y, month_of, eof_variance=0.999, var_order=1, copula="gaussian", rng=rng)
    sim = dep.simulate(month_of, np.random.default_rng(3))
    cw = np.corrcoef(sim[winter, 0], sim[winter, 1])[0, 1]
    cs = np.corrcoef(sim[~winter, 0], sim[~winter, 1])[0, 1]
    assert cw > 0.4                                               # winter coupling restored
    assert cw - cs > 0.3                                          # and it is seasonal, not smeared
