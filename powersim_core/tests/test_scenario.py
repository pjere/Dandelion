"""Tests for the scenario-workbook accessor + snapshot (§7)."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from powersim_core import scenario


@pytest.fixture
def workbook(tmp_path):
    p = tmp_path / "scenarios.xlsx"
    with pd.ExcelWriter(p) as xw:
        pd.DataFrame({"year": [2030], "variable": ["gdp"], "value": [1.2]}).to_excel(
            xw, sheet_name="demand_macro", index=False)
        pd.DataFrame({"unit_id": ["N1"], "capacity_mw": [900.0]}).to_excel(
            xw, sheet_name="avail_fleet_registry", index=False)
        pd.DataFrame({"farm": ["A"], "latitude": [51.0]}).to_excel(
            xw, sheet_name="res_offshore_farms", index=False)
    return p


def test_load_model_sheets_strips_prefix(workbook):
    demand = scenario.load_model_sheets(workbook, "demand")
    assert set(demand) == {"macro"}                          # prefix stripped, only this model's tabs
    assert demand["macro"]["variable"].iloc[0] == "gdp"
    assert set(scenario.load_model_sheets(workbook, "avail")) == {"fleet_registry"}


def test_load_sheet_single(workbook):
    df = scenario.load_sheet(workbook, "res", "offshore_farms")
    assert list(df["farm"]) == ["A"]


def test_unknown_prefix_raises(workbook):
    with pytest.raises(ValueError, match="no 'weather_"):
        scenario.load_model_sheets(workbook, "weather")


def test_snapshot_freezes_parquet_and_manifest(workbook, tmp_path):
    mpath = scenario.snapshot(workbook, out_dir=tmp_path / "snap")
    manifest = json.loads(mpath.read_text())
    assert manifest["sha256"] == scenario.file_sha256(workbook)
    assert set(manifest["tabs"]) == {"demand_macro", "avail_fleet_registry", "res_offshore_farms"}
    # frozen parquet round-trips
    back = pd.read_parquet(tmp_path / "snap" / "avail_fleet_registry.parquet")
    assert back["capacity_mw"].iloc[0] == 900.0
