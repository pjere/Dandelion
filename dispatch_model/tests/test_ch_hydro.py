"""`io.ch_hydro` — repairing an ENTSO-E series that was under-reported until a coverage fix.

Swiss run-of-river reads 1.95/1.85/2.13/2.27 TWh in 2019/2022/2023/2024 and 14.50 TWh in 2025, with
declared capacity flat across the step. ROR is must-take and peaks on snowmelt, which is exactly when Swiss
prices go negative, so the missing five sixths of the fleet is why the model printed 15 negative hours
against 613 observed.

The properties pinned here are the ones whose failure would be silent: repairing a year that needs no
repair, asserting more capacity than the fleet has, or inventing generation for a zone with no evidence.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from dispatch_model.io import ch_hydro as ch


class _Cfg:
    pass


@pytest.fixture(autouse=True)
def _clear_cache():
    """`ch_hydro._CACHE` is module-level and keyed on (db_key, zone, year). Every test here uses the same
    `_Cfg` stub, so without this one test's patched series is served to the next from cache — which is a
    silent cross-test dependency that only surfaces under `pytest-randomly`'s ordering (it did: a
    no-evidence case was handed a cached 1400 MW series and failed one run in two)."""
    ch._CACHE.clear()
    yield
    ch._CACHE.clear()


def _patch(monkeypatch, per_year: dict[int, np.ndarray]):
    """`_tech_series` returns the metered series for a year; None where the year is absent."""
    def fake(config, zones, year, tech):
        v = per_year.get(int(year))
        if v is None:
            return None
        idx = pd.date_range(f"{year}-01-01", periods=len(v), freq="h", tz="UTC")
        return pd.Series(v, index=idx)
    monkeypatch.setattr(ch, "_tech_series", fake)


def _flat(mw: float, n: int = 8760):
    return np.full(n, mw, float)


def test_the_complete_year_is_never_repaired(monkeypatch):
    """2025 is the anchor. Scaling it would double-count the very fleet it measures."""
    _patch(monkeypatch, {2024: _flat(260.0), 2025: _flat(1660.0)})
    assert ch.coverage_factor(_Cfg(), "CH", "hydro_ror", 2025) is None
    assert ch.coverage_factor(_Cfg(), "CH", "hydro_ror", 2026) is None


def test_a_partial_year_is_scaled_to_the_complete_year(monkeypatch):
    _patch(monkeypatch, {2022: _flat(210.0), 2023: _flat(240.0), 2024: _flat(260.0), 2025: _flat(1660.0)})
    cf = ch.coverage_factor(_Cfg(), "CH", "hydro_ror", 2024)
    assert cf is not None
    k, envelope = cf
    assert 6.0 < k < 8.0, f"expected ~7x coverage factor, got {k:.2f}"
    assert envelope == pytest.approx(1660.0)


def test_the_reconstruction_cannot_exceed_the_measured_envelope(monkeypatch):
    """A scale factor on an erratic peak can otherwise assert capacity the fleet does not have — the
    residual-based first version peaked at 4.99 GW against a fleet whose complete feed peaks at 3.67."""
    spiky = _flat(260.0)
    spiky[100] = 980.0                                   # 2024's real peak, 2x the other partial years
    _patch(monkeypatch, {2022: _flat(210.0), 2023: _flat(240.0), 2024: spiky, 2025: _flat(1660.0)})
    add = ch.missing_mw(_Cfg(), "CH", 2024)
    assert add is not None
    total = spiky + add.to_numpy()
    assert total.max() <= 1660.0 + 1e-6, f"reconstruction exceeded the envelope: {total.max():.0f}"


def test_no_evidence_means_no_reconstruction(monkeypatch):
    _patch(monkeypatch, {})                              # no series at all
    assert ch.missing_mw(_Cfg(), "CH", 2024) is None
    _patch(monkeypatch, {2024: _flat(260.0)})            # no complete year to anchor on
    assert ch.coverage_factor(_Cfg(), "CH", "hydro_ror", 2024) is None


def test_a_series_that_needs_no_repair_is_left_alone(monkeypatch):
    """k must exceed a threshold before anything is asserted — ordinary year-to-year variation is not a
    coverage break, and the sweep behind `_COVERAGE_FIXED` found 16 of 26 candidate steps to be exactly
    that."""
    _patch(monkeypatch, {2023: _flat(1600.0), 2024: _flat(1620.0), 2025: _flat(1660.0)})
    assert ch.coverage_factor(_Cfg(), "CH", "hydro_ror", 2024) is None


def test_only_listed_zone_techs_are_touched(monkeypatch):
    assert ch.coverage_factor(_Cfg(), "FR", "hydro_ror", 2024) is None
    assert ch.coverage_factor(_Cfg(), "CH", "gas", 2024) is None
    idx = pd.date_range("2024-01-01", periods=24, freq="h", tz="UTC")
    df = pd.DataFrame({"load_mw": 7000.0, "musttake_res_mw": 500.0}, index=idx)
    assert ch.apply_to_netload(_Cfg(), "FR", 2024, df) is df


def test_apply_adds_to_musttake_and_not_to_load(monkeypatch):
    """It must price. `gb_embedded` nets its residual off LOAD precisely so it cannot; Swiss ROR is the
    opposite case — it is what spills in a surplus, and the zone's bid ladder should set the price."""
    idx = pd.date_range("2024-01-01", periods=24, freq="h", tz="UTC")
    df = pd.DataFrame({"load_mw": 7000.0, "musttake_res_mw": 500.0}, index=idx)
    monkeypatch.setattr(ch, "missing_mw", lambda *a, **k: pd.Series(1200.0, index=idx))
    out = ch.apply_to_netload(_Cfg(), "CH", 2024, df)
    assert (out["musttake_res_mw"] == 1700.0).all()
    assert (out["load_mw"] == 7000.0).all(), "load must be untouched"


def test_flag_off_is_a_noop(monkeypatch):
    monkeypatch.setenv("DISPATCH_CH_ROR", "0")
    idx = pd.date_range("2024-01-01", periods=24, freq="h", tz="UTC")
    df = pd.DataFrame({"load_mw": 7000.0, "musttake_res_mw": 500.0}, index=idx)
    assert ch.apply_to_netload(_Cfg(), "CH", 2024, df) is df
