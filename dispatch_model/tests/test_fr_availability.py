"""#160 — step-v FR nuclear/reservoir availability mapping (pure; the lake loader itself is DB-backed)."""
from __future__ import annotations

import datetime as dt

import pandas as pd
from dispatch_model.rolling.fr_availability import nuclear_unavail_daily, reservoir_budget_mwh


def _fa(frac_value=0.8, year=2030, week_budget=None):
    idx = pd.MultiIndex.from_tuples([(0, year, d) for d in range(1, 366)], names=["draw", "year", "doy"])
    frac = pd.Series(frac_value, index=idx)
    return {"nuc_frac": frac, "nuc_draws": 1, "reservoir": (week_budget or {})}


def test_nuclear_unavail_maps_fraction_to_outage_on_reference_calendar():
    fa = _fa(frac_value=0.8)
    nu = nuclear_unavail_daily(fa, target_year=2030, ref_year=2019, draw=0, nuc_cap=50000.0)
    # keyed by REFERENCE-year (2019) dates, full year
    assert dt.date(2019, 1, 1) in nu and dt.date(2019, 12, 31) in nu
    assert len(nu) == 365
    # outage = (1 − 0.8) × 50 000 = 10 000 MW at every day
    assert all(abs(v - 10000.0) < 1e-6 for v in nu.values())


def test_nuclear_unavail_draw_wrap_and_absent_cases():
    fa = _fa()
    # draw 3 wraps mod nuc_draws (=1) → draw 0 → same result, not None
    assert nuclear_unavail_daily(fa, 2030, 2019, 3, 50000.0) is not None
    # a year the lake does not cover → None (projection keeps its proxy)
    assert nuclear_unavail_daily(fa, 2099, 2019, 0, 50000.0) is None
    # non-positive capacity → None
    assert nuclear_unavail_daily(fa, 2030, 2019, 0, 0.0) is None


def test_reservoir_budget_lookup_by_iso_week():
    wk = pd.Timestamp("2030-01-28", tz="UTC")
    w = int(wk.isocalendar().week)
    fa = _fa(week_budget={(2030, w): 4_000_000.0})
    assert reservoir_budget_mwh(fa, 2030, wk) == 4_000_000.0
    # a week with no budget entry → None (window keeps its reference-year reservoir energy)
    assert reservoir_budget_mwh(fa, 2030, pd.Timestamp("2030-06-01", tz="UTC")) is None
    # no reservoir data at all → None
    assert reservoir_budget_mwh(_fa(), 2030, wk) is None
