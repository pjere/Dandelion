"""DM Phase 4 offline test: the residual model recovers persistence + heteroscedasticity
and reproduces its own draws under a fixed seed."""
from __future__ import annotations

import numpy as np
import pandas as pd
from demand_model.residual.model import _buckets, fit_residual_model


def _synthetic_resid(n=24 * 500, phi=0.85, seed=0):
    """AR(1) noise scaled up in winter/evening — a known heteroscedastic, persistent residual."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2018-01-01", periods=n, freq="h", tz="UTC")
    z = np.zeros(n)
    eta = rng.standard_t(5, n)                     # fat-tailed innovations
    for t in range(1, n):
        z[t] = phi * z[t - 1] + eta[t]
    local = idx.tz_convert("Europe/Paris")
    scale = 300 + 700 * np.isin(local.month, [12, 1, 2]) + \
        400 * ((local.hour.to_numpy() >= 18) & (local.hour.to_numpy() <= 21))
    return pd.Series(z * scale, index=idx)


def test_recovers_persistence_and_scale():
    resid = _synthetic_resid()
    rm = fit_residual_model(resid, order=2, seed=42)
    # AR persistence recovered (sum of phi near the true 0.85)
    assert 0.7 < float(np.sum(rm.phi)) < 0.95
    # heteroscedasticity: winter σ clearly exceeds summer σ
    winter = [k for k in rm.sigma.index if k.startswith("DJF")]
    summer = [k for k in rm.sigma.index if k.startswith("JJA")]
    assert rm.sigma[winter].mean() > 1.5 * rm.sigma[summer].mean()
    # simulated series matches empirical scale and lag-1 autocorrelation
    assert 0.8 < rm.metrics["std_ratio_sim_over_emp"] < 1.25
    assert abs(rm.metrics["acf1_simulated"] - rm.metrics["acf1_empirical"]) < 0.1


def test_seed_reproducible_and_stable():
    resid = _synthetic_resid()
    rm = fit_residual_model(resid, order=2, seed=1)
    idx = pd.date_range("2030-01-01", periods=24 * 30, freq="h", tz="UTC")
    a = rm.simulate(idx, n_paths=3, seed=7)
    b = rm.simulate(idx, n_paths=3, seed=7)
    c = rm.simulate(idx, n_paths=3, seed=8)
    assert np.allclose(a.to_numpy(), b.to_numpy())        # same seed -> identical
    assert not np.allclose(a.to_numpy(), c.to_numpy())    # different seed -> different
    assert np.isfinite(a.to_numpy()).all()                # AR stable, no blow-up


def test_buckets_are_local_time():
    idx = pd.date_range("2020-06-01", periods=48, freq="h", tz="UTC")
    keys = _buckets(idx)
    assert len(set(keys)) > 1 and all("|" in k for k in keys)
