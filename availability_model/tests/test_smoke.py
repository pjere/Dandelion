"""AVAIL Phase 0 smoke tests: config loads, fleet registry builds, workbook round-trips, meta works."""
from __future__ import annotations

import pytest
from availability_model.config import load_config
from availability_model.io.assumptions import build_template, load_assumptions
from availability_model.meta import run_metadata


def _cfg():
    return load_config("config.yaml")


def _have_db(cfg):
    return cfg.resolve(cfg.section("data")["sqlite_path"]).exists()


def test_config_loads():
    cfg = _cfg()
    assert cfg.seed > 0
    assert cfg.section("assumptions")["forced_correction_target"] in ("slope", "level")
    assert cfg.section("calibration")["baseline_exclude"]           # 2021-23 excluded from baseline


def test_fleet_registry_and_workbook(tmp_path):
    cfg = _cfg()
    if not _have_db(cfg):
        pytest.skip("pricemodeling DB not present")
    from availability_model.io.fleet import build_fleet_registry
    reg = build_fleet_registry(cfg)
    nuc = reg[reg["technology"] == "nuclear"]
    assert len(nuc) >= 50                                            # ~56 reactors
    assert nuc["palier"].notna().all()                              # every reactor mapped to a palier
    assert {"CP0", "CPY", "P4", "P'4", "N4", "EPR"} == set(nuc["palier"])
    assert (nuc["capacity_mw"] > 800).all()                         # ≥900 MW reactors
    assert (nuc["capacity_mw"] < 1800).all()                         # ≤ EPR ~1650 MW: no aggregate rows
    assert 55_000 < nuc["capacity_mw"].sum() < 70_000               # FR nuclear fleet ≈ 61 GW
    # river-cooled reactors flagged for weather derating; coastal ones not
    assert (nuc["cooling"] == "river").any() and (nuc["cooling"] == "sea").any()

    wb = tmp_path / "assumptions_avail.xlsx"
    build_template(cfg, wb)
    sheets = load_assumptions(wb)                                    # validates every sheet
    assert {"fleet_registry", "planned_outage_params", "forced_outage_params", "common_mode",
            "weather_derating", "interconnectors", "hydro_inflows"} <= set(sheets)
    fo = sheets["forced_outage_params"]
    assert "user_correction_pct" in set(fo["variable"])             # the ±10% knob exists
    cm = sheets["common_mode"]
    assert "event_freq_per_year" in set(cm["variable"])             # common-mode calibratable


def test_metadata_stamps():
    cfg = _cfg()
    m = run_metadata(cfg, weather_draw=3, seed=42)
    assert m["seed"] == 42 and m["weather_draw"] == "3" and "config_hash" in m
