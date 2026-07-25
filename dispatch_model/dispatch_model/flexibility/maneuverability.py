"""Nuclear maneuverability from fuel-cycle position (FLEX-F1) — the C4 end-of-cycle rigidity input.

A reactor between two refuelling outages runs one *fuel cycle*. Its load-following capability depends on
where it sits in that cycle (control-margin / xénon head-room shrink as the fuel depletes):
  - first ~92 % of the cycle → **full** maneuverability;
  - the pre-stretch weeks (~92–98 %) → **reduced** (deep-mod caps and band halved in the LP);
  - the final **stretch-out** (fin de campagne, ~last 2 %) → **none**: the unit is must-run at a *declining*
    coast-down power and cannot modulate; its only alternative is a full shutdown until the next refuelling.

The cycle boundaries are the refuelling outages (ASR / VP / VD). This module is a **pure function of an
outage calendar** `[unit_id, start, end, outage_type]` — fed by `planned_scheduler.schedule_planned()` in
projection, and by `availability_model.io.outages.infer_outage_events` (observed) in the backtest — so both
paths share one derivation. Output is **per unit-week** (the dispatch window granularity): a flag + a
stretch-out power fraction the FLEX LP (F3, C4) consumes.
"""
from __future__ import annotations

import pandas as pd

from . import reactor_class as rc

_REFUEL = frozenset({"ASR", "VP", "VD"})     # refuelling outages bound the fuel cycles (forced/maint don't)

# cycle length (months) by reactor class — 900 MW annual-ish, the larger paliers ~18-month campaigns. Used
# to project the open boundary of the leading/trailing cycle when the calendar doesn't bracket it.
_CYCLE_MONTHS = {"900": 12.0, "1300": 18.0, "N4": 18.0, "EPR": 18.0, "EPR2": 18.0}


def cycle_days(palier=None, capacity_mw=None) -> float:
    return _CYCLE_MONTHS[rc.class_name(palier, capacity_mw)] * 30.44


def _cycles_for_unit(starts, ends, cyc_days: float) -> list[tuple]:
    """Fuel cycles (cycle_start, cycle_end) from a unit's sorted refuelling outages: each cycle spans the
    *end* of one refuelling to the *start* of the next. The leading/trailing open cycles are projected out
    by `cyc_days` so a unit observed mid-campaign still gets a stretch-out timing."""
    cyc: list[tuple] = []
    for i in range(1, len(starts)):
        cyc.append((ends[i - 1], starts[i]))            # between refuel i-1 (end) and refuel i (start)
    if len(starts):
        cyc.insert(0, (starts[0] - pd.Timedelta(days=cyc_days), starts[0]))     # leading (project back)
        cyc.append((ends[-1], ends[-1] + pd.Timedelta(days=cyc_days)))          # trailing (project fwd)
    return cyc


def _flag_at(t, cyc: list[tuple], pre_stretch: float, stretch: float, coast_floor: float):
    """maneuverability + stretch power at instant `t`. Outside any cycle (i.e. during an outage) → full;
    the unit is unavailable there anyway (avail=0), so the flag is moot."""
    for cs, ce in cyc:
        span = (ce - cs) / pd.Timedelta(days=1)
        if span <= 0 or not (cs <= t < ce):
            continue
        frac = ((t - cs) / pd.Timedelta(days=1)) / span
        if frac < pre_stretch:
            return "full", 1.0
        if frac < stretch:
            return "reduced", 1.0
        sp = 1.0 - (1.0 - coast_floor) * (frac - stretch) / max(1.0 - stretch, 1e-9)   # coast-down
        return "none", float(sp)
    return "full", 1.0


def derive_weekly(outages: pd.DataFrame, units: pd.DataFrame, week_starts,
                  pre_stretch: float = 0.92, stretch: float = 0.98, coast_floor: float = 0.90) -> pd.DataFrame:
    """→ [unit_id, week_start, maneuverability ∈ {full,reduced,none}, stretch_power] per unit-week.

    `outages`: calendar `[unit_id, start, end, outage_type]` (tz-aware). `units`: `[unit_id, capacity_mw]`
    (+ optional `palier`). `week_starts`: the window starts (tz-aware). Evaluated at each week's midpoint."""
    weeks = pd.DatetimeIndex(week_starts)
    mid = weeks + pd.Timedelta(days=3, hours=12)
    ref = outages[outages["outage_type"].isin(_REFUEL)] if not outages.empty else outages
    by_unit = {uid: g.sort_values("start") for uid, g in ref.groupby("unit_id")} if not ref.empty else {}
    rows = []
    for u in units.itertuples(index=False):
        uid = u.unit_id
        cyc_days = cycle_days(getattr(u, "palier", None), getattr(u, "capacity_mw", None))
        g = by_unit.get(uid)
        cyc = _cycles_for_unit(list(g["start"]), list(g["end"]), cyc_days) if g is not None else []
        for w, m in zip(weeks, mid):
            flag, sp = _flag_at(m, cyc, pre_stretch, stretch, coast_floor) if cyc else ("full", 1.0)
            rows.append({"unit_id": uid, "week_start": w, "maneuverability": flag, "stretch_power": sp})
    return pd.DataFrame(rows)


def backtest_calendar(con, start: str, end: str,
                      nominal_min: float = 850.0, nominal_max: float = 1700.0) -> pd.DataFrame:
    """FR nuclear refuelling calendar for the backtest, from the stored REMIT outage messages
    (`entsoe_unavailability`). Planned outages of nuclear-range units bound the fuel cycles; permanent-
    closure sentinels (end far in the future) and short planned maintenance (<15 days) are dropped. Returns
    the calendar `[unit_id, capacity_mw, start, end, outage_type]` (outage_type collapsed to a refuelling
    marker). `con` is any DB-API/SQLAlchemy connection usable by ``pandas.read_sql``."""
    q = ("SELECT unit_name AS unit_id, nominal_mw, start_utc, end_utc FROM entsoe_unavailability "
         "WHERE outage_type='planned' AND nominal_mw BETWEEN ? AND ? AND start_utc < ? AND end_utc > ?")
    df = pd.read_sql(q, con, params=(nominal_min, nominal_max, end, start))
    cols = ["unit_id", "capacity_mw", "start", "end", "outage_type"]
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["start"] = pd.to_datetime(df["start_utc"], utc=True)
    df["end"] = pd.to_datetime(df["end_utc"], utc=True)
    df = df[df["end"].dt.year < 2090]                                      # drop permanent-closure sentinels
    df = df[(df["end"] - df["start"]).dt.days >= 15]                       # refuelling ≥ ~2 weeks
    df["capacity_mw"] = df["nominal_mw"].astype(float)
    df["outage_type"] = "VP"                                              # collapse to a refuelling marker
    return df[cols].sort_values(["unit_id", "start"]).reset_index(drop=True)


def units_from_calendar(calendar: pd.DataFrame) -> pd.DataFrame:
    """Distinct `[unit_id, capacity_mw]` (max nominal per unit) — the `units` frame for ``derive_weekly``."""
    if calendar.empty:
        return pd.DataFrame(columns=["unit_id", "capacity_mw"])
    return (calendar.groupby("unit_id", as_index=False)["capacity_mw"].max())


def fleet_distribution(weekly: pd.DataFrame) -> dict:
    """Sanity metric: share of unit-weeks in each maneuverability state (fleet-level)."""
    if weekly.empty:
        return {}
    vc = weekly["maneuverability"].value_counts(normalize=True)
    return {k: round(float(vc.get(k, 0.0)), 3) for k in ("full", "reduced", "none")}
