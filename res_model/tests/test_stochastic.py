"""RES Phase 5 tests: residual model is heteroscedastic-by-CF, AR-persistent, cross-tech correlated,
bounded and reproducible."""
from __future__ import annotations

import numpy as np
import pandas as pd
from res_model.stochastic.model import ResidualModel


def _model():
    edges = np.linspace(0, 1, 11)                       # 10 bins
    # inverted-U σ over CF level (small at 0 and 1, large mid) — the wind power-curve signature
    mid = 1 - (np.linspace(0.05, 0.95, 10) - 0.5) ** 2 / 0.25
    sig = {"wind": 0.15 * mid, "pv": 0.10 * mid}
    return ResidualModel(
        technologies=["wind", "pv"], order=2, bin_edges=edges,
        sigma=sig, sigma_global={"wind": 0.1, "pv": 0.07},
        phi={"wind": np.array([0.9, -0.1]), "pv": np.array([0.7, 0.0])},
        innov_std={"wind": 0.4, "pv": 0.6},
        corr=np.array([[1.0, 0.5], [0.5, 1.0]]), cf_max=1.0)


def _panel(n=24 * 200):
    idx = pd.date_range("2030-01-01", periods=n, freq="h", tz="UTC")
    rng = np.random.default_rng(0)
    return pd.DataFrame({"wind": np.clip(rng.random(n), 0, 1),
                         "pv": np.clip(0.5 * np.sin(np.arange(n) / 12) ** 2, 0, 1)}, index=idx)


def test_reproducible_bounded():
    m, cf = _model(), _panel()
    a = m.simulate(cf, n_paths=1, seed=7)["wind"]["path_000"]
    b = m.simulate(cf, n_paths=1, seed=7)["wind"]["path_000"]
    c = m.simulate(cf, n_paths=1, seed=8)["wind"]["path_000"]
    assert np.allclose(a, b) and not np.allclose(a, c)          # seeded
    assert (a >= 0).all() and (a <= 1.0).all()                 # clipped to [0, cf_max]


def test_heteroscedastic_by_cf():
    m = _model()
    n = 60000
    idx = pd.date_range("2030-01-01", periods=n, freq="h", tz="UTC")
    # constant-CF panels at a mid level vs an edge level
    mid = pd.DataFrame({"wind": np.full(n, 0.5), "pv": np.full(n, 0.5)}, index=idx)
    edge = pd.DataFrame({"wind": np.full(n, 0.02), "pv": np.full(n, 0.02)}, index=idx)
    rmid = m.simulate(mid, seed=1)["wind"]["path_000"] - 0.5
    redge = m.simulate(edge, seed=1)["wind"]["path_000"] - 0.02
    assert rmid.std() > 2 * redge.std()                        # mid-curve noise >> edge noise


def test_ar_persistence_and_cross_tech():
    m, cf = _model(), _panel()
    sim = m.simulate(cf, n_paths=1, seed=3)
    rw = (sim["wind"]["path_000"] - cf["wind"]).to_numpy()
    rp = (sim["pv"]["path_000"] - cf["pv"]).to_numpy()
    # AR persistence in the wind residual
    assert np.corrcoef(rw[:-1], rw[1:])[0, 1] > 0.5
    # cross-tech correlation shows up (corr set to 0.5)
    assert np.corrcoef(rw, rp)[0, 1] > 0.2
