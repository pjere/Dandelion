"""DM Phase 0 smoke — config loads, the assumptions workbook builds and re-validates,
and the data contracts accept/reject correctly. No DB or network."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from demand_model.config import load_config
from demand_model.io.assumptions import build_template, load_assumptions
from demand_model.io.schemas import LOAD_HIST, validate

CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"


def test_config_loads():
    cfg = load_config(CONFIG)
    assert cfg.seed == 20260707
    assert cfg.section("perimeter")["subtract_pumping"] is True
    assert cfg.section("perimeter")["resolution"] == "1h"


def test_workbook_template_roundtrip(tmp_path):
    wb = build_template(tmp_path / "assumptions.xlsx")
    assert wb.exists()
    sheets = load_assumptions(wb)
    # required driver families present and tidy
    for s in ("demography", "macro", "residential_tertiary", "mobility",
              "new_large_loads", "efficiency", "btm_pv", "weights"):
        assert s in sheets
    demo = sheets["demography"]
    assert {"year", "variable", "unit", "value", "scenario"} <= set(demo.columns)
    assert (demo["variable"] == "population").all()
    # EV charging profiles sum to ~1 over 24h
    prof = sheets["profiles"]
    tot = prof.groupby("profile")["value"].sum()
    assert ((tot - 1.0).abs() < 1e-3).all()


def test_load_contract_rejects_bad_units():
    good = pd.DataFrame({
        "timestamp_utc": pd.date_range("2020-01-01", periods=3, freq="h", tz="UTC"),
        "load_mw": [50000.0, 51000.0, 49000.0],
    })
    assert len(validate(good, LOAD_HIST, "load")) == 3
    bad = good.copy(); bad.loc[0, "load_mw"] = 5_000_000  # MWh-as-MW blunder -> out of range
    with pytest.raises(ValueError):
        validate(bad, LOAD_HIST, "load")
