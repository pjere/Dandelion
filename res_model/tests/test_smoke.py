"""RES Phase 0 smoke tests: config loads, workbook builds + validates, schemas + meta work."""
from __future__ import annotations

import pandas as pd
from res_model.config import load_config
from res_model.io.assumptions import build_template, load_assumptions
from res_model.io.schemas import WEATHER, validate
from res_model.meta import run_metadata


def test_config_loads():
    cfg = load_config("config.yaml")
    assert cfg.seed > 0
    assert set(cfg.section("perimeter")["technologies"]) == {"pv", "wind_onshore", "wind_offshore", "hydro_ror"}
    assert cfg.section("perimeter")["output_basis"] == "potential"      # curtailment is step vi


def test_workbook_roundtrip(tmp_path):
    wb = tmp_path / "assumptions_res.xlsx"
    build_template(wb)
    sheets = load_assumptions(wb)                                       # validates every sheet
    cap = sheets["capacity_trajectories"]
    assert {"pv_utility", "pv_distributed", "pv_btm", "wind_onshore",
            "wind_offshore_fixed", "wind_offshore_floating"} <= set(cap["technology"])
    # PV segments exposed separately (perimeter consistency with step iii)
    assert cap[cap["technology"].str.startswith("pv_")]["technology"].nunique() == 3
    farms = sheets["offshore_farms"]
    assert (farms["capacity_mw"] > 0).all() and {"fixed", "floating"} <= set(farms["foundation"])


def test_weather_contract_accepts_synthetic_shape():
    df = pd.DataFrame({
        "timestamp_utc": pd.date_range("2027-01-01", periods=3, freq="h", tz="UTC"),
        "station_id": ["07005"] * 3,
        "temperature_c": [5.0, 5.1, 4.9], "wind_speed_ms": [6.0, 7.2, 5.5],
        "cloud_cover_pct": [80.0, 75.0, 90.0], "precip_1h_mm": [0.0, 0.1, 0.0],
    })
    out = validate(df, WEATHER, "weather")
    assert len(out) == 3


def test_run_metadata_stamps():
    cfg = load_config("config.yaml")
    m = run_metadata(cfg, weather_draw=3, seed=42)
    assert m["seed"] == 42 and m["weather_draw"] == "3"
    assert "config_hash" in m and "git_hash" in m
