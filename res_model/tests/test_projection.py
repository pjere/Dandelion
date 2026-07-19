"""RES Phase 6 tests: vintage factor rises with newer cohorts; capacity interp; Projector coherence
+ PV double-count reconciliation (integration, skipped if artifacts absent)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from res_model.config import load_config
from res_model.projection.vintage import annual_capacity, fleet_cf_factor


def _sheets():
    cap = pd.DataFrame([
        {"technology": "wind_onshore", "region": "FR", "year": y, "capacity_mw": 20000 + (y - 2025) * 1500,
         "scenario": "reference"} for y in range(2025, 2047)])
    vint = pd.DataFrame([
        {"technology": "wind_onshore", "cohort_year": 2010, "variable": "cf_uplift_vs_legacy", "value": 0.0, "scenario": "reference"},
        {"technology": "wind_onshore", "cohort_year": 2020, "variable": "cf_uplift_vs_legacy", "value": 0.10, "scenario": "reference"},
        {"technology": "wind_onshore", "cohort_year": 2035, "variable": "cf_uplift_vs_legacy", "value": 0.25, "scenario": "reference"},
    ])
    return {"capacity_trajectories": cap, "technology_vintages": vint}


def test_annual_capacity():
    cap = annual_capacity(_sheets(), "wind_onshore")
    assert cap.loc[2025] == 20000 and cap.loc[2046] > cap.loc[2025]


def test_fleet_factor_rises_with_new_cohorts():
    years = np.arange(2025, 2047)
    fac = fleet_cf_factor(_sheets(), "wind_onshore", years)
    assert fac.loc[2025] >= 1.0
    assert fac.loc[2046] > fac.loc[2025]                     # newer high-CF cohorts lift the fleet CF
    assert 1.0 <= fac.loc[2046] <= 1.25                      # bounded by cohort uplifts


def test_fleet_factor_flat_without_vintages():
    s = _sheets(); s["technology_vintages"] = s["technology_vintages"].iloc[:0]
    fac = fleet_cf_factor(s, "wind_onshore", np.arange(2025, 2047))
    assert np.allclose(fac.to_numpy(), 1.0)


def test_projection_integration():
    cfg = load_config("config.yaml")
    if not (cfg.models_dir / "calibrated_res.json").exists() or \
       not (cfg.models_dir / "residual_res.json").exists() or \
       not (cfg.resolve(cfg.section("weather")["weathergen_output"])).exists():
        pytest.skip("calibration/residual/cube artifacts not present")
    from res_model.projection import Projector
    pj = Projector(cfg)
    a = pj.production("reference", realization=0, seed=0)
    b = pj.production("reference", realization=0, seed=0)
    assert np.allclose(a["national_total"], b["national_total"])          # seeded/coherent
    assert (a["national_total"] >= 0).all() and np.isfinite(a["national_total"]).all()
    # PV segments reconcile to the total (no double count)
    assert np.allclose(a["pv_total"], a[["pv_utility", "pv_distributed", "pv_btm"]].sum(axis=1))
    dc = pj.double_count_report("reference", 0)
    assert dc["reconciled"] and dc["btm_pv_generation_twh"] > 0
