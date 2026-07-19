"""Phase 2 — common-mode (generic-fault) calibration from the 2021–23 stress-corrosion episode.

This is the module that decides the upper price quantiles. Its calibration target is the *excess*
nuclear unavailability the generic fault produced ON TOP OF the routine baseline — NOT the raw count of
long outages in the window (in any ~2-year window almost every reactor has a routine VP/VD, so counting
those would spuriously say "96 % of the fleet was hit"). We reconstruct the daily fleet-unavailability
trajectory, subtract the non-crisis baseline, and read off the excess pulse (peak depth, ramp, plateau,
recovery). That pulse is exactly the fleet capacity the Phase-4 module must take offline when a
common-mode event fires — it reproduces the observed 2022 low (~0.54 vs ~0.74 baseline).

The event *frequency* is not estimable from a single episode, so it is pinned to the user's
return-period band (§ validation, D5); the empirical 1-in-N is reported alongside for context.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import Config
from ..io.outages import daily_capacity_factor


def _fleet_unavailability(cf: pd.DataFrame, crisis) -> pd.DataFrame:
    """Daily fraction of observed nuclear units on full outage, with a crisis flag."""
    daily = cf.groupby("day")["is_outage"].mean().rename("unavail").reset_index()
    inc = np.zeros(len(daily), dtype=bool)
    for cs, ce in crisis:
        inc |= (daily["day"] >= cs) & (daily["day"] <= ce)
    daily["in_crisis"] = inc
    return daily


def calibrate_common_mode(events: pd.DataFrame, registry: pd.DataFrame, config: Config) -> dict:
    ret = config.section("validation")["common_mode_return_years"]
    period = config.section("data")["period"]
    obs_years = (pd.Timestamp(period["end"]) - pd.Timestamp(period["start"])).days / 365.25
    crisis = [(pd.Timestamp(w["start"]), pd.Timestamp(w["end"]))
              for w in config.section("calibration").get("baseline_exclude", [])]

    nuc_reg = registry[registry["technology"] == "nuclear"]
    fleet_op = int(nuc_reg[nuc_reg["closure_year"].isna()].shape[0]) or int(nuc_reg.shape[0])
    cf = daily_capacity_factor(config, nuc_reg)
    daily = _fleet_unavailability(cf, crisis).sort_values("day")
    daily["unavail_30d"] = daily["unavail"].rolling(30, min_periods=10, center=True).mean()

    baseline = float(daily.loc[~daily["in_crisis"], "unavail"].median())
    excess = (daily["unavail_30d"] - baseline).clip(lower=0)
    cr = daily["in_crisis"].to_numpy()
    peak_excess = float(np.nanmax(excess.to_numpy()[cr])) if cr.any() else 0.0
    # pulse widths: days where the crisis excess exceeds half the peak
    half = excess.to_numpy() >= 0.5 * peak_excess
    plateau_days = int(np.sum(half & cr))

    # which paliers drove the excess: crisis outage-rate vs baseline outage-rate, per palier
    pal = registry.set_index("unit_id")["palier"]
    cfp = cf.assign(palier=cf["unit_id"].map(pal),
                    in_crisis=lambda d: _flag(d["day"], crisis))
    tp = {}
    for palier, g in cfp.dropna(subset=["palier"]).groupby("palier"):
        base = g.loc[~g["in_crisis"], "is_outage"].mean()
        cris = g.loc[g["in_crisis"], "is_outage"].mean()
        tp[str(palier)] = max(0.0, float(cris - base))              # excess outage rate during crisis
    tot = sum(tp.values()) or 1.0
    target_prob = {k: round(v / tot, 3) for k, v in tp.items()}

    mid_return = 0.5 * (ret["low"] + ret["high"])
    return {
        "event_freq_per_year": round(1.0 / mid_return, 4),
        "empirical_return_years": round(obs_years, 1),             # ≈ single observed episode over the window
        "baseline_unavail": round(baseline, 3),
        "peak_excess_unavail": round(peak_excess, 3),              # fleet fraction offline ON TOP of baseline
        "implied_crisis_availability": round(1 - baseline - peak_excess, 3),  # ≈ observed 2022 low (~0.54)
        "plateau_days": plateau_days,
        "stagger_weeks_mean": round(min(plateau_days, 180) / 7 / 2, 1),   # half the plateau as cascade spread
        "target_prob": target_prob,
        "fleet_op": fleet_op,
        "return_years_target": [ret["low"], ret["high"]],
    }


def _flag(days: pd.Series, crisis) -> np.ndarray:
    inc = np.zeros(len(days), dtype=bool)
    d = days.to_numpy()
    for cs, ce in crisis:
        inc |= (d >= np.datetime64(cs)) & (d <= np.datetime64(ce))
    return inc
