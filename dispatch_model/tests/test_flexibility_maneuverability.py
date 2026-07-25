"""FLEX-F1 — maneuverability from fuel-cycle position (pure function, no DB)."""
from __future__ import annotations

import pandas as pd
from dispatch_model.flexibility import maneuverability as mv


def _ts(s):
    return pd.Timestamp(s, tz="UTC")


# N1 fuel cycle runs from the end of a refuel (2024-01-01) to the start of the next (2024-12-15).
_OUTAGES = pd.DataFrame([
    {"unit_id": "N1", "start": _ts("2023-11-15"), "end": _ts("2024-01-01"), "outage_type": "VP"},
    {"unit_id": "N1", "start": _ts("2024-12-15"), "end": _ts("2025-01-20"), "outage_type": "ASR"},
    {"unit_id": "N1", "start": _ts("2024-05-01"), "end": _ts("2024-05-04"), "outage_type": "forced"},  # ignored
])
_UNITS = pd.DataFrame({"unit_id": ["N1", "N2"], "capacity_mw": [900.0, 1300.0]})
_WEEKS = [_ts("2024-02-05"), _ts("2024-06-03"), _ts("2024-11-25"), _ts("2024-12-09")]  # Mondays


def _flag(df, uid, week):
    r = df[(df["unit_id"] == uid) & (df["week_start"] == _ts(week))].iloc[0]
    return r["maneuverability"], r["stretch_power"]


def test_cycle_position_maps_to_maneuverability():
    df = mv.derive_weekly(_OUTAGES, _UNITS, _WEEKS)
    assert _flag(df, "N1", "2024-02-05")[0] == "full"       # ~11 % through the cycle
    assert _flag(df, "N1", "2024-06-03")[0] == "full"       # ~45 %
    assert _flag(df, "N1", "2024-11-25")[0] == "reduced"    # ~95 % (pre-stretch)
    flag, sp = _flag(df, "N1", "2024-12-09")                # ~99 % (stretch-out)
    assert flag == "none" and sp < 1.0                      # must-run at declining coast-down power


def test_forced_outage_does_not_bound_a_cycle():
    # the short forced outage in May must not split the cycle — June still reads full
    df = mv.derive_weekly(_OUTAGES, _UNITS.iloc[[0]], _WEEKS)
    assert _flag(df, "N1", "2024-06-03")[0] == "full"


def test_unit_without_refuelling_is_full_everywhere():
    df = mv.derive_weekly(_OUTAGES, _UNITS, _WEEKS)
    assert (df[df["unit_id"] == "N2"]["maneuverability"] == "full").all()
    assert (df[df["unit_id"] == "N2"]["stretch_power"] == 1.0).all()


def test_fleet_distribution_sums_and_is_mostly_full():
    df = mv.derive_weekly(_OUTAGES, _UNITS, _WEEKS)
    dist = mv.fleet_distribution(df)
    assert abs(sum(dist.values()) - 1.0) < 1e-6
    assert dist["full"] >= 0.5                              # most unit-weeks are load-following-capable
