"""AVAIL Phase 7 — validation suite. Runs the suite once on a modest draw count."""
from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def val(cfg):
    if not cfg.resolve(cfg.section("data")["weathergen_output"]).exists():
        pytest.skip("weathergen cube not present")
    from availability_model.pipeline import run_validation
    return run_validation(cfg, n_draws=8)


def test_no_hard_failures(val):
    assert val["summary"]["fail"] == 0
    assert val["summary"]["pass"] >= 5


def test_noncrisis_kd_passes(val):
    c = next(c for c in val["checks"] if c["check"] == "noncrisis_nuclear_Kd")
    assert c["status"] == "PASS"


def test_return_period_and_seasonality(val):
    by = {c["check"]: c["status"] for c in val["checks"]}
    assert by["common_mode_return_period"] == "PASS"
    assert by["planned_summer_seasonality"] == "PASS"


def test_reports_written(cfg, val):
    assert (cfg.reports_dir / "validation_report.json").exists()
    assert (cfg.reports_dir / "methodology.md").exists()
