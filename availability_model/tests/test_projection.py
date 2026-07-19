"""AVAIL Phase 6 — projection engine assembly + outputs. Runs a small (2-draw) projection once."""
from __future__ import annotations

import json

import numpy as np
import pytest


@pytest.fixture(scope="module")
def proj(cfg):
    if not cfg.resolve(cfg.section("data")["weathergen_output"]).exists():
        pytest.skip("weathergen cube not present")
    from availability_model.pipeline import project
    by_tech = project(cfg, n_draws=2)
    return cfg, by_tech


def test_outputs_written(proj):
    from powersim_core import lake
    cfg, _ = proj
    for ds in ("availability_by_tech", "availability_nuclear_units", "interconnectors", "reservoir_budget"):
        assert lake.exists("availability", ds), ds           # outputs now land in the lake (§6)
    meta = json.loads((cfg.output_dir / "run_metadata.json").read_text())
    assert meta["n_draws"] == 2 and "config_hash" in meta and "weather_cube_hash" in meta


def test_available_within_capacity(proj):
    cfg, by_tech = proj
    reg = __import__("availability_model.projection.engine", fromlist=["load_scenario_registry"]) \
        .load_scenario_registry(cfg)
    cap_by_tech = reg.groupby("technology")["capacity_mw"].sum()
    assert (by_tech["available_mw"] >= -1e-6).all()                 # never negative
    peak = by_tech.groupby("technology")["available_mw"].max()
    for t, mx in peak.items():
        assert mx <= cap_by_tech[t] + 1.0                          # never exceeds installed capacity


def test_nuclear_kd_in_band(proj):
    cfg, by_tech = proj
    reg = __import__("availability_model.projection.engine", fromlist=["load_scenario_registry"]) \
        .load_scenario_registry(cfg)
    nuc_cap = reg.loc[(reg["technology"] == "nuclear") & reg["closure_year"].isna(), "capacity_mw"].sum()
    kd = by_tech[by_tech["technology"] == "nuclear"].groupby("draw")["available_mw"].mean() / nuc_cap
    assert (kd.between(0.72, 0.80)).all()                          # anchored to historical ~0.74


def test_nuclear_units_states_and_availability(proj):
    from powersim_core import lake
    cfg, _ = proj
    df = lake.read_table("availability", "availability_nuclear_units")
    assert set(df["state"].unique()) <= {"available", "planned", "forced", "common_mode", "derated"}
    assert (df["available_mw"] >= -1e-6).all()
    # offline states carry zero available capacity; available/derated carry >0
    off = df[df["state"].isin(["planned", "forced", "common_mode"])]
    assert (off["available_mw"].abs() < 1e-6).all()


def test_assemble_reproducible(cfg):
    if not cfg.resolve(cfg.section("data")["weathergen_output"]).exists():
        pytest.skip("weathergen cube not present")
    from availability_model.calibration.model import CalibratedAvailability
    from availability_model.io.weather import load_national_weather
    from availability_model.projection.engine import _assemble_draw, _horizon, load_scenario_registry
    model = CalibratedAvailability.load(cfg.models_dir / "calibrated_availability.json")
    reg = load_scenario_registry(cfg)
    temp, _ = load_national_weather(cfg)
    hstart, days, nd = _horizon(cfg)
    a, _, _ = _assemble_draw(cfg, model, reg, 1, temp, hstart, days, nd)
    b, _, _ = _assemble_draw(cfg, model, reg, 1, temp, hstart, days, nd)
    assert np.array_equal(a, b)                                     # deterministic given (seed, draw)
