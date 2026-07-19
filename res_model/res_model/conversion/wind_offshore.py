"""Phase 3 — offshore wind conversion (§5.3), farm-level.

Sites are known (workbook), so offshore is modelled farm-by-farm: offshore-100 m wind (from the D1
coastal→offshore transfer) → the farm's turbine-class power curve (lower specific power ⇒ higher,
flatter CF than onshore) with a modest multi-turbine smear, wake + electrical losses, availability.
Verified against published French offshore CFs (~35–42 %) in Phase 4/7.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .wind_onshore import _V, aggregate_power_curve


def offshore_farm_cf(wind: pd.Series, specific_power: float = 350.0, smoothing_ms: float = 1.5,
                     availability: float = 0.94, wake_loss: float = 0.10,
                     cut_in: float = 3.0, cut_out: float = 30.0) -> pd.Series:
    """Per-unit offshore capacity factor for one farm from its hub-height wind series."""
    curve = aggregate_power_curve(specific_power, smoothing_ms, cut_in, cut_out)
    cf = np.interp(wind.to_numpy(), _V, curve)
    cf = cf * availability * (1.0 - wake_loss)
    return pd.Series(np.clip(cf, 0.0, 1.0), index=wind.index, name="offshore_cf")
