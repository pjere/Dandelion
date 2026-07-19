"""DM Phase 5 — bottom-up new-load modules (added on top of the rescaled statistical core).

All functions return an hourly MW Series aligned to the projection index.

* ``ev_load``            — fleet × km × kWh/km energy, shaped by charging archetypes (smart vs home)
* ``flat_new_loads``    — electrolysis + datacentres + other point loads (flat baseload approx)
* ``btm_pv_netting``    — behind-the-meter PV self-consumption to SUBTRACT. Only PV added *after*
                          the anchor year is netted: RTE REALISED already excludes today's BTM-PV.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .drivers import Drivers

_HOURS_YEAR = 8760.0


def _year_of(index: pd.DatetimeIndex) -> np.ndarray:
    return index.year.to_numpy()


def _map_year(index: pd.DatetimeIndex, yearly: pd.Series) -> np.ndarray:
    """Broadcast a per-year Series onto every timestamp (clipped to the driver horizon)."""
    yr = np.clip(_year_of(index), int(yearly.index.min()), int(yearly.index.max()))
    return yearly.reindex(yr).to_numpy()


def _profile(drivers: Drivers, name: str) -> np.ndarray:
    """24-value local-hour shape summing to 1 (from the workbook 'profiles' sheet)."""
    prof = drivers.sheets.get("profiles")
    if prof is None or name not in set(prof["profile"]):
        p = np.ones(24)
    else:
        p = prof[prof["profile"] == name].set_index("hour")["value"].reindex(range(24)).fillna(0.0).to_numpy()
    s = p.sum()
    return p / s if s > 0 else np.full(24, 1 / 24)


def ev_load(drivers: Drivers, index: pd.DatetimeIndex, cfg_proj: dict) -> pd.Series:
    """EV charging load (MW). Annual energy per segment, shaped by a smart/home charging mix."""
    seg_cfg = cfg_proj.get("ev_segments", {})
    energy_kwh = pd.Series(0.0, index=drivers.years, dtype=float)   # per horizon year
    fleet_var = {"car": "ev_fleet_cars", "lcv": "ev_fleet_lcv", "hgv": "ev_fleet_hgv"}
    for seg, spec in seg_cfg.items():
        fleet = drivers.at("mobility", fleet_var[seg])
        km = (drivers.at("mobility", spec["km_per_year_var"]) if "km_per_year_var" in spec
              else pd.Series(spec["km_per_year"], index=drivers.years))
        kwh = (drivers.at("mobility", spec["kwh_per_km_var"]) if "kwh_per_km_var" in spec
               else pd.Series(spec["kwh_per_km"], index=drivers.years))
        energy_kwh = energy_kwh.add(fleet * km * kwh, fill_value=0.0)

    avg_mw = energy_kwh / (_HOURS_YEAR * 1000.0)                    # MW, yearly average
    smart = drivers.at("mobility", "smart_charging_share")
    home_p, smart_p = _profile(drivers, "home_evening"), _profile(drivers, "smart_offpeak")

    local_h = index.tz_convert("Europe/Paris").hour.to_numpy()
    avg_vec = _map_year(index, avg_mw)
    smart_vec = _map_year(index, smart)
    # hourly-multiplier vs daily average = 24 · profile[h]; mix by the smart-charging share
    shape = 24.0 * (smart_vec * smart_p[local_h] + (1.0 - smart_vec) * home_p[local_h])
    return pd.Series(avg_vec * shape, index=index, name="ev")


def flat_new_loads(drivers: Drivers, index: pd.DatetimeIndex) -> pd.Series:
    """Electrolysis + datacentres + other point loads (MW), flat within each year."""
    elec = drivers.at("new_large_loads", "electrolysis_capacity") * \
        drivers.at("new_large_loads", "electrolysis_load_factor") * 1000.0
    dc = drivers.at("new_large_loads", "datacentre_load") * 1000.0
    other = drivers.at("new_large_loads", "other_pointload") * 1000.0
    total = (elec + dc + other)
    return pd.Series(_map_year(index, total), index=index, name="new_loads")


def btm_pv_netting(drivers: Drivers, ghi_nat: pd.Series, performance_ratio: float) -> pd.Series:
    """Self-consumed BTM-PV to SUBTRACT (MW). Only capacity added after the anchor year is netted."""
    cap = drivers.series("btm_pv", "btm_pv_capacity")
    incr_cap = (cap - cap.loc[drivers.anchor_year]).clip(lower=0.0).reindex(drivers.years)   # GW added
    ratio = drivers.at("btm_pv", "self_consumption_ratio")
    index = ghi_nat.index
    gen = _map_year(index, incr_cap) * 1000.0 * (ghi_nat.to_numpy() / 1000.0) * performance_ratio
    self_cons = gen * _map_year(index, ratio)
    return pd.Series(np.clip(self_cons, 0.0, None), index=index, name="btm_pv")
