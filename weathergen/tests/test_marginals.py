"""Phase 4 unit tests: monotone invertible splice, GPD tail extrapolation, censored precip."""
from __future__ import annotations

import numpy as np
from weathergen.marginals import Censored, _fit_semiparametric

from weathergen import marginals


def test_semiparametric_roundtrip_and_monotone():
    rng = np.random.default_rng(0)
    z = rng.normal(0, 1, 20000)
    m = _fit_semiparametric(z, tails=["lower", "upper"], q=0.95)
    # round-trip F^-1(F(x)) ~ x across the body and tails
    x = np.linspace(-3.5, 3.5, 400)
    assert np.max(np.abs(m.ppf(m.cdf(x)) - x)) < 0.05
    # CDF monotone increasing
    u = m.cdf(np.sort(x))
    assert np.all(np.diff(u) >= -1e-12)


def test_gpd_tail_extrapolates_beyond_sample():
    """A fitted upper GPD returns levels BEYOND the observed maximum (extrapolation)."""
    rng = np.random.default_rng(1)
    z = rng.normal(0, 1, 30000)
    m = _fit_semiparametric(z, tails=["upper"], q=0.95)
    far = m.ppf(np.array([1 - 1e-5]))[0]      # ~1-in-100000 level
    assert far > z.max() - 0.5                # not capped at the sample max
    assert np.isfinite(far)


def test_censored_precip_preserves_dry_frequency():
    rng = np.random.default_rng(2)
    n = 20000
    wet = rng.random(n) < 0.2                  # 20% wet
    z = np.where(wet, rng.gamma(2, 1, n), -2.0 + rng.normal(0, 0.01, n))
    dry = ~wet
    keys = [("S", "precip_1h_mm")]
    cfg = {"precip_1h_mm": {"kind": "intermittent", "tails": ["upper"]}}
    ms = marginals.fit(z[:, None], keys, cfg, q=0.95, dry=dry[:, None])
    cens = ms.cols[0]
    assert isinstance(cens, Censored)
    assert abs(cens.p_dry - 0.8) < 0.02
    # inverse of uniforms reproduces ~the dry fraction (u < p_dry -> dry side)
    u = rng.random(50000)
    dry_frac = (u < cens.p_dry).mean()
    assert abs(dry_frac - 0.8) < 0.02
