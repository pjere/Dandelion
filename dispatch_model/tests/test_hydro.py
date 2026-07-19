"""DISP Phase 6 — reservoir energy budget + endogenous water value (dual of the budget cap)."""
from __future__ import annotations

import pandas as pd
import pytest
from dispatch_model.lp.single_zone import solve_window

_STACK = pd.DataFrame([
    ("NUC", "nuclear", 40000, 7.0, 0.0),
    ("GAS", "gas", 20000, 60.0, 0.0),
    ("RES", "hydro_reservoir", 10000, 0.0, 0.0),
], columns=["unit_id", "tech", "capacity_mw", "srmc_eur_mwh", "min_gen_frac"])


def test_reservoir_budget_and_water_value():
    T = pd.date_range("2024-01-01", periods=24, freq="h", tz="UTC")
    D = [30000] * 12 + [55000] * 12                       # peak needs gas even with reservoir
    budget = 60000                                        # 6 peak-hours worth of 10 GW
    out = solve_window(T, D, [0] * 24, _STACK, energy_caps={"hydro_reservoir": budget})
    res = out["dispatch"].query("unit_id=='RES'").set_index("time")["output_mw"].reindex(T)

    assert abs(res.sum() - budget) < 1e-6                 # budget binds
    assert res.iloc[:12].sum() < 1e-6                     # nothing off-peak (saved for peak)
    assert res.iloc[12:].sum() > budget - 1e-6           # all spent in peak (peak-shaving)
    # water value = marginal value of +1 MWh budget = the gas it displaces in peak
    assert abs(out["water_values"]["hydro_reservoir"] - 60.0) < 1e-6
    assert out["price"].iloc[-1] == 60                    # gas marginal in peak


def test_no_cap_means_no_water_value():
    T = pd.date_range("2024-01-01", periods=4, freq="h", tz="UTC")
    out = solve_window(T, [45000] * 4, [0] * 4, _STACK)   # no energy_caps
    assert out["water_values"] == {}


def test_reservoir_climatology_from_db():
    from dispatch_model.config import load_config
    from dispatch_model.hydro.guide_curves import annual_wetness, reservoir_generation_climatology
    cfg = load_config("config.yaml")
    if not cfg.resolve(cfg.section("data")["sqlite_path"]).exists():
        pytest.skip("pricemodeling DB not present")
    clim = reservoir_generation_climatology(cfg)
    assert len(clim) == 52 and (clim > 0).all()
    # French reservoir hydro is higher in winter/autumn than mid-summer trough
    assert clim.loc[1:8].mean() > clim.loc[28:34].mean()
    w = annual_wetness(cfg, 2019)
    assert 0.5 < w < 1.6
