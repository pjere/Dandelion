"""Phase 4 — stochastic forced-outage process (per unit, all technologies).

Each unit draws forced outages as a Poisson process whose annual rate carries the calibrated frequency,
a calendar trend with the user's ±10 % correction on the SLOPE (D2), and an age-creep term. Durations
are heavy-tailed (lognormal). A forced outage that would land inside a planned outage is dropped — the
unit is already offline — which realistically thins the forced rate when planned load is high.

Deterministic given (seed, draw). Nuclear frequency here is the "multi-day full" rate from calibration;
total nuclear unavailability is anchored by planned + this + common-mode, checked in Phase 7.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from powersim_core.rng import substream

from ..calibration.model import CalibratedAvailability
from ..config import Config

_AGE_CREEP_PCT_YR = 0.3                                             # frequency creep per year of unit age


def _planned_intervals(planned: pd.DataFrame) -> dict[str, list[tuple]]:
    if planned is None or planned.empty:
        return {}
    out: dict[str, list[tuple]] = {}
    for uid, g in planned.groupby("unit_id"):
        out[uid] = list(zip(g["start"], g["end"]))
    return out


def simulate_forced(config: Config, model: CalibratedAvailability, registry: pd.DataFrame,
                    draw: int = 0, planned: pd.DataFrame | None = None,
                    user_corrections: dict | None = None) -> pd.DataFrame:
    proj = config.section("projection")
    y0, y1 = proj["horizon"]["start_year"], proj["horizon"]["end_year"]
    target = config.section("assumptions").get("forced_correction_target", "slope")
    per = config.section("data")["period"]
    ref_year = (pd.Timestamp(per["start"]).year + pd.Timestamp(per["end"]).year) // 2  # calibration midpoint
    uc = user_corrections or {}
    rng = substream(config.seed, draw, "forced")          # F4: single RNG authority (SeedSequence)
    pintervals = _planned_intervals(planned)

    rows = []
    for _, u in registry.iterrows():
        fp = model.forced.get(u["technology"])
        if not fp or pd.notna(u["closure_year"]):
            continue
        base = float(fp["freq_per_unit_year"])
        slope = float(fp.get("trend_slope_pct_yr", 0.0))
        corr = float(uc.get(u["technology"], 0.0))                  # ±10% user correction (D2)
        eff_slope = slope * (1 + corr / 100) if target == "slope" else slope
        level_factor = (1 + corr / 100) if target == "level" else 1.0
        mu, sig = fp["dur_lognorm_mu"], fp["dur_lognorm_sigma"]
        planned_u = pintervals.get(u["unit_id"], [])
        for y in range(y0, y1 + 1):
            # age creep accrues only BEYOND the calibration midpoint (the base rate already reflects the
            # fleet's age during calibration); otherwise old reactors would be double-aged at y0.
            extra_age = max(0, y - ref_year)
            rate = base * (1 + eff_slope / 100 * (y - y0)) * (1 + _AGE_CREEP_PCT_YR / 100 * extra_age) * level_factor
            n = rng.poisson(max(0.0, rate))
            for _ in range(n):
                start = pd.Timestamp(year=y, month=1, day=1, tz="UTC") + pd.Timedelta(
                    days=float(rng.uniform(0, 365)))
                if any(s <= start <= e for s, e in planned_u):     # already in planned outage → drop
                    continue
                dur = float(np.clip(np.exp(rng.normal(mu, sig)), 0.5, 120.0))
                rows.append({"unit_id": u["unit_id"], "name": u["name"], "technology": u["technology"],
                             "state": "forced", "start": start, "end": start + pd.Timedelta(days=dur),
                             "duration_days": round(dur, 2), "capacity_mw": float(u["capacity_mw"]),
                             "draw": int(draw)})
    cols = ["unit_id", "name", "technology", "state", "start", "end", "duration_days", "capacity_mw", "draw"]
    return (pd.DataFrame(rows, columns=cols).sort_values("start").reset_index(drop=True)
            if rows else pd.DataFrame(columns=cols))
