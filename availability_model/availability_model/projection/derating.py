"""Phase 5 — weather-linked thermal derating of river/estuary-cooled units.

Driven by the SAME weather draw as demand (iii) and RES (iv): on hot days a river-cooled reactor loses
output to river-temperature regulatory limits, so a heat wave simultaneously pushes demand up and this
availability down — the coupling that matters for summer price spikes. The derate is a deterministic
transform of the shared temperature series (no RNG): available fraction =
    1 − clip(derate_frac_per_c × max(0, T_lagged − threshold), 0, max_derate)
with river temperature approximated by a lagged air temperature (`water_lag_weeks`). Only sensitive
basins (river/estuary) derate; sea/tower cooling is unaffected. Output is sparse — only unit-days that
actually derate are returned.
"""
from __future__ import annotations

import pandas as pd

from ..calibration.model import CalibratedAvailability
from ..config import Config

_MAX_DERATE = 0.30                                                  # regulatory cap: beyond this the unit trips


def thermal_derating(config: Config, model: CalibratedAvailability, registry: pd.DataFrame,
                     temp_daily: pd.Series) -> pd.DataFrame:
    """Per unit-day available fraction (<1) on derating days.

    `temp_daily` is a daily (national) air-temperature series for the weather draw; basin-specific
    temperature is a documented refinement (French heat waves are large-scale, so the national driver
    already captures the coupling). Returns long df [unit_id, day, avail_frac] for derated unit-days only.
    """
    der = model.derating
    temp_daily = temp_daily.sort_index()
    sens = registry[(registry["technology"] == "nuclear") & registry["basin"].notna()].copy()
    rows = []
    for basin, g in sens.groupby("basin"):
        p = der.get(str(basin))
        if not p or not p.get("sensitive") or p["derate_frac_per_c"] <= 0:
            continue
        lag = int(round(float(p.get("water_lag_weeks", 1.5)) * 7))
        t_lag = temp_daily.rolling(max(1, lag), min_periods=1).mean()   # river temp ~ lagged air temp
        excess = (t_lag - float(p["air_temp_threshold_c"])).clip(lower=0)
        derate = (float(p["derate_frac_per_c"]) * excess).clip(0, _MAX_DERATE)
        hot = derate[derate > 0]
        if hot.empty:
            continue
        avail = 1.0 - hot
        for uid in g["unit_id"]:
            rows.append(pd.DataFrame({"unit_id": uid, "day": hot.index, "avail_frac": avail.to_numpy()}))
    if not rows:
        return pd.DataFrame(columns=["unit_id", "day", "avail_frac"])
    return pd.concat(rows, ignore_index=True).sort_values(["unit_id", "day"]).reset_index(drop=True)
