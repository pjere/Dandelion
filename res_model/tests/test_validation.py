"""RES Phase 7 tests: Diag status logic + HTML render; integration (skipped if artifacts absent)."""
from __future__ import annotations

import pytest
from res_model.config import load_config
from res_model.validation.suite import Diag, _render


def test_diag_status():
    assert Diag("a", "c", "d", passed=True).status == "PASS"
    assert Diag("a", "c", "d", passed=False).status == "FAIL"
    assert Diag("a", "c", "d", passed=False, soft=True).status == "WARN"
    assert Diag("a", "c", "d", passed=None).status == "INFO"


def test_render_html():
    html = _render([Diag("x", "Cat", "detail", passed=True),
                    Diag("y", "Cat", "d2", passed=False, soft=True)])
    assert "<html>" in html and "PASS" in html and "WARN" in html and "res_model" in html


def test_validation_suite_runs():
    cfg = load_config("config.yaml")
    if not (cfg.models_dir / "calibrated_res.json").exists():
        pytest.skip("calibration artifacts not present")
    from res_model.validation import run_validation_suite
    diags = run_validation_suite(cfg)
    cats = {d.category for d in diags}
    assert "Calibration" in cats and any("killer" in c for c in cats)
    assert (cfg.reports_dir / "validation_report.html").exists()
