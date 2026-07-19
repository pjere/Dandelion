"""Neighbour stochastic availability (#80): mean-preserving multiplier, tech mapping, capacity scaling."""
from __future__ import annotations

import numpy as np
import pandas as pd
from dispatch_model.neighbour_availability import (
    apply_multipliers,
    availability_multipliers,
)

_STATS = {
    "Nuclear": {"mean_avail": 0.72, "std_avail": 0.06, "installed_mw": 60000},
    "Fossil Gas": {"mean_avail": 0.90, "std_avail": 0.03, "installed_mw": 10000},
    "Fossil Hard coal": {"mean_avail": 0.85, "std_avail": 0.00, "installed_mw": 5000},  # zero spread
    "Solar": {"mean_avail": 0.99, "std_avail": 0.01, "installed_mw": 40000},            # unmapped → skipped
}


def test_multiplier_is_mean_preserving_and_maps_techs():
    rng = np.random.default_rng(0)
    draws = [availability_multipliers(_STATS, rng) for _ in range(4000)]
    nuc = np.array([d["nuclear"] for d in draws])
    assert 0.98 < nuc.mean() < 1.02                 # E[multiplier] ≈ 1 (no level shift → no double-count)
    assert nuc.std() > 0.03                          # carries the REMIT spread (0.06/0.72 ≈ 0.083)
    assert "gas" in draws[0] and "coal" in draws[0]
    assert "Solar" not in draws[0] and all("solar" not in d for d in draws)   # unmapped tech skipped


def test_zero_spread_tech_gives_unity():
    rng = np.random.default_rng(1)
    m = availability_multipliers(_STATS, rng)
    assert m["coal"] == 1.0                          # std 0 → exactly 1.0


def test_multiplier_is_clipped():
    wild = {"Nuclear": {"mean_avail": 0.5, "std_avail": 0.5, "installed_mw": 1000}}
    rng = np.random.default_rng(2)
    vals = np.array([availability_multipliers(wild, rng)["nuclear"] for _ in range(2000)])
    assert vals.min() >= 0.6 and vals.max() <= 1.15


def test_apply_multipliers_scales_capacity():
    stack = pd.DataFrame({"tech": ["nuclear", "gas", "gas", "biomass"],
                          "capacity_mw": [1000.0, 400.0, 600.0, 200.0]})
    out = apply_multipliers(stack, {"nuclear": 0.8, "gas": 1.1})
    assert out.loc[out["tech"] == "nuclear", "capacity_mw"].iloc[0] == 800.0
    assert out.loc[out["tech"] == "gas", "capacity_mw"].sum() == pytest_approx(1100.0)
    assert out.loc[out["tech"] == "biomass", "capacity_mw"].iloc[0] == 200.0   # untouched
    assert stack.loc[0, "capacity_mw"] == 1000.0                                # original unchanged


def pytest_approx(x, rel=1e-6):
    import pytest
    return pytest.approx(x, rel=rel)
