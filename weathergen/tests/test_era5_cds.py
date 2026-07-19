"""Phase 1 — CDS request builder (no network)."""
from __future__ import annotations

from pathlib import Path

from weathergen.config import load_config

from weathergen import era5_cds

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"


def test_build_request_shape():
    cfg = load_config(CONFIG_PATH)
    dataset, req = era5_cds.build_request(cfg, 2015, months=[6])
    assert dataset == "reanalysis-era5-single-levels"
    # the 8 mapped variables become CDS long names
    assert "2m_temperature" in req["variable"]
    assert "10m_u_component_of_wind" in req["variable"]
    assert "total_precipitation" in req["variable"]
    assert req["year"] == ["2015"] and req["month"] == ["06"]
    assert len(req["time"]) == 24 and len(req["day"]) == 31
    # area = [N, W, S, E] from the metropole bbox
    assert req["area"] == [52.0, -6.0, 41.0, 10.0]
    assert req["data_format"] == "netcdf"
