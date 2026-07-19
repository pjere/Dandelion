"""Phase 3 — run-of-river hydro (§9 Q4), the 4th weather-driven module.

ROR follows river flow, which integrates catchment precipitation with weeks-to-months of memory plus a
spring snowmelt pulse. We model a daily inflow index = exponentially-weighted accumulated national
precipitation, map it to a capacity factor around a seasonal baseline, and smooth to daily resolution.
Baseline / sensitivity / memory are calibrated to the observed national ROR CF (~40 %) in Phase 4.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ror_cf(precip_nat: pd.Series, baseline: float = 0.40, memory_halflife_d: float = 30.0,
           sensitivity: float = 0.5, snowmelt_amp: float = 0.10, cf_min: float = 0.05,
           cf_max: float = 0.80) -> pd.Series:
    """Hourly ROR capacity factor from an hourly national precipitation series (mm).

    ``sensitivity`` scales the response to inflow anomalies; ``snowmelt_amp`` adds a spring bump."""
    precip_nat = precip_nat.sort_index()
    daily = precip_nat.resample("1D").sum()
    inflow = daily.ewm(halflife=memory_halflife_d).mean()            # catchment memory
    mean = inflow.mean()
    anom = inflow / mean - 1.0 if mean > 0 else inflow * 0.0

    doy = daily.index.dayofyear.to_numpy()
    snow = snowmelt_amp * np.exp(-0.5 * ((doy - 135) / 40.0) ** 2)   # spring (~mid-May) snowmelt pulse
    cf_daily = np.clip(baseline * (1.0 + sensitivity * anom.to_numpy()) + snow, cf_min, cf_max)
    cf_daily = pd.Series(cf_daily, index=daily.index)

    # map daily CF back onto the hourly index (ROR varies slowly → hold within the day)
    day_key = precip_nat.index.normalize()
    cf_h = cf_daily.reindex(cf_daily.index.normalize().union(day_key)).ffill().reindex(
        pd.Index(day_key)).to_numpy()
    return pd.Series(np.clip(cf_h, cf_min, cf_max), index=precip_nat.index, name="ror_cf")
