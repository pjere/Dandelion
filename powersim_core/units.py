"""Unit-conversion helpers (§5) — one implementation, so MW/MWh confusion (a classic-bug-checklist item)
has a single audited home. Power (MW) integrated over hours gives energy (MWh); at hourly resolution
1 MW for 1 h = 1 MWh, but conversions are named explicitly to prevent silent resample errors.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MWH_PER_MW_HOUR = 1.0
GWH_PER_MWH = 1e-3
TWH_PER_MWH = 1e-6
_OIL_MWH_TH_PER_BBL = 1.7            # ~1.7 MWh_th per barrel of Brent


def energy_mwh_from_power_mw(power_mw, hours: float = 1.0):
    """Energy (MWh) from average power (MW) over `hours` (default hourly grid → 1 h)."""
    return power_mw * hours


def annual_twh(power_mw: pd.Series) -> float:
    """Annual energy (TWh) from an hourly MW series."""
    return float(np.nansum(power_mw)) * TWH_PER_MWH


def brent_usd_bbl_to_eur_mwh_th(usd_per_bbl: float, usd_per_eur: float = 1.08) -> float:
    return usd_per_bbl / _OIL_MWH_TH_PER_BBL / usd_per_eur


def celsius_from_kelvin(k):
    return k - 273.15
