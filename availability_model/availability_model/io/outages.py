"""Phase 1 — infer the historical outage-event catalogue from per-unit production (D1).

A unit is "on outage" on a day when its daily capacity factor collapses to ~0 (CF ≤ `outage_cf_max`)
for a sustained stretch. We deliberately key off *full* outages, not every dip below rated power:
nuclear load-follows and idles economically at reduced power, so a 0.90 threshold would drown the
signal in false positives. Full-outage runs (CF ≤ 0.05) are unambiguous and are what a planned/forced
outage model needs to calibrate frequency and duration.

On economic idling (important): reactor modulation / load-following lives at CF ≈ 0.2–0.9, well above
0.05, so it is counted as AVAILABLE, not as an outage — the inferred availability is therefore a
technical-availability proxy, not a load factor (a reactor throttled by the market is still 100%
available; step vi dispatch decides what is economically called). The residual leak is *sustained
economic near-zero* operation (hot-standby over a multi-day low-demand / negative-price spell), which
can enter the short "forced" class → treat inferred forced frequency as an UPPER BOUND, cross-checked
against literature EFOR in Phase 2. Partial derating (one system down, ~50% output) is counted as
available and thus under-counted — a separate, opposite-direction limitation.

Events are classified by duration against the ASR/VP/VD day-bands (§ config.calibration):
    dur ≤ forced_max_days              → forced
    nuclear, else ≤60 / ≤120 / >120    → ASR / VP / VD   (refuelling / visite partielle / décennale)
    non-nuclear, else                  → maintenance
Runs are split by data gaps (conservative) and by the required minimum length. Right-censoring: a run
touching a unit's last observation is flagged; for permanently-closed units (closure_year set, e.g.
Fessenheim) it is dropped rather than counted as a ~1000-day outage.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import Config
from .production import daily_unit_output
from .schemas import OUTAGE_EVENTS, validate


def _classify(tech: str, dur: float, forced_max: float) -> str:
    if dur <= forced_max:
        return "forced"
    if tech == "nuclear":
        return "ASR" if dur <= 60 else ("VP" if dur <= 120 else "VD")
    return "maintenance"


def daily_capacity_factor(config: Config, registry: pd.DataFrame) -> pd.DataFrame:
    """Per unit-day CF + state → [unit_id, day, mean_mw, hours, cf, is_outage].

    A day counts toward outage detection only if hour coverage is adequate; sparse days are marked
    `is_outage=False` so a data gap can never masquerade as (or bridge) an outage.
    """
    d = config.section("data")
    inf = d["inference"]
    min_hours = int(24 * 0.5)                                        # ≥12h of samples to judge a day
    out = daily_unit_output(config)
    cap = registry.set_index("unit_id")["capacity_mw"]
    out = out[out["unit_id"].isin(cap.index)].copy()
    out["cf"] = out["mean_mw"] / out["unit_id"].map(cap)
    out["is_outage"] = (out["cf"] <= float(inf["outage_cf_max"])) & (out["hours"] >= min_hours)
    return out.reset_index(drop=True)


def infer_outage_events(config: Config, registry: pd.DataFrame) -> pd.DataFrame:
    """Build the inferred outage-event catalogue (validated against OUTAGE_EVENTS)."""
    cal = config.section("calibration")
    inf = config.section("data")["inference"]
    forced_max = float(cal["forced_max_days"])
    min_days = int(inf["min_outage_days"])
    crisis = [(pd.Timestamp(w["start"]), pd.Timestamp(w["end"])) for w in cal.get("baseline_exclude", [])]

    inferable = set(config.section("data")["inference"].get("inferable_techs", ["nuclear"]))
    registry = registry[registry["technology"].isin(inferable)]     # must-run units only (see config)
    cf = daily_capacity_factor(config, registry)
    meta = registry.set_index("unit_id")[["technology", "capacity_mw", "closure_year"]]
    events = []
    for uid, g in cf.groupby("unit_id", sort=False):
        g = g.sort_values("day")
        last_obs = g["day"].max()
        o = g[g["is_outage"]]
        if o.empty:
            continue
        # consecutive calendar-day runs: a gap >1 day starts a new spell
        brk = (o["day"].diff().dt.days.fillna(1) > 1).cumsum()
        tech = meta.at[uid, "technology"]
        closed = pd.notna(meta.at[uid, "closure_year"])
        cap_mw = float(meta.at[uid, "capacity_mw"])
        for _, run in o.groupby(brk):
            start, end = run["day"].min(), run["day"].max()
            dur = float((end - start).days + 1)                     # calendar span
            if run.shape[0] < min_days or dur < min_days:
                continue
            censored = end >= last_obs
            if censored and closed:                                 # permanent closure, not an outage
                continue
            in_crisis = any(start <= ce and cs <= end for cs, ce in crisis)
            events.append({
                "unit_id": uid, "start": start.tz_localize("UTC"), "end": end.tz_localize("UTC"),
                "duration_days": dur, "outage_type": _classify(tech, dur, forced_max),
                "capacity_mw": cap_mw, "technology": tech, "mean_cf": float(run["cf"].mean()),
                "n_outage_days": int(run.shape[0]), "censored": bool(censored), "in_crisis": in_crisis,
            })
    ev = pd.DataFrame(events)
    if ev.empty:
        return validate(ev, OUTAGE_EVENTS, "outage_events")
    ev = ev.sort_values(["unit_id", "start"]).reset_index(drop=True)
    return validate(ev, OUTAGE_EVENTS, "outage_events")


def availability_summary(config: Config, registry: pd.DataFrame) -> pd.DataFrame:
    """Per-technology technical-availability proxy: 1 − mean(full-outage day) over observed unit-days.

    Computed straight from the daily-CF frame so the numerator and denominator are always the same
    day set. This is the outage-based availability (Kd-like), distinct from the production load factor
    which is depressed by economic modulation. Split crisis vs non-crisis so Phase 2/7 can check the
    ~0.75 non-crisis / ~0.54 2022 nuclear bands (§ validation)."""
    cf = daily_capacity_factor(config, registry).merge(
        registry[["unit_id", "technology"]], on="unit_id", how="left")
    crisis = [(pd.Timestamp(w["start"]), pd.Timestamp(w["end"]))
              for w in config.section("calibration").get("baseline_exclude", [])]
    in_crisis = np.zeros(len(cf), dtype=bool)
    for cs, ce in crisis:
        in_crisis |= (cf["day"] >= cs) & (cf["day"] <= ce)
    cf["in_crisis"] = in_crisis

    inferable = set(config.section("data")["inference"].get("inferable_techs", ["nuclear"]))
    rows = []
    for tech, g in cf.groupby("technology"):
        exc = g[~g["in_crisis"]]
        rows.append({"technology": tech, "inferable": tech in inferable,
                     "observed_unit_days": int(len(g)),
                     "outage_unit_days": int(g["is_outage"].sum()),
                     # 'availability' is a true availability proxy ONLY where inferable=True; elsewhere
                     # it is 1 − idle-fraction (an economic load factor) and must not be read as Kd.
                     "availability_all": float(1 - g["is_outage"].mean()),
                     "availability_ex_crisis": float(1 - exc["is_outage"].mean()) if len(exc) else np.nan})
    return pd.DataFrame(rows).sort_values(["inferable", "technology"], ascending=[False, True]).reset_index(drop=True)
