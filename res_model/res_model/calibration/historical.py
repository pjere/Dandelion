"""Phase 4 — modelled national capacity factors over the historical period (uncalibrated).

Runs the physical chains on the historical weather so they can be matched to observed CFs:
  PV       — cloud→GHI per station → pv_cf (default cohort) → national mean
  onshore  — ERA5-100 m national-mean wind → onshore_cf (default curve)
  offshore — ERA5-100 m at farm points → offshore_farm_cf, capacity-weighted
  hydro    — national precip → ror_cf
Cached to Parquet (the PV solar-geometry pass is the expensive part).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from powersim_core import lake

from ..config import Config
from ..conversion.hydro_ror import ror_cf
from ..conversion.pv import PVCohort, pv_cf
from ..conversion.wind_offshore import offshore_farm_cf
from ..conversion.wind_onshore import onshore_cf
from ..transfer.ghi import ghi_from_cloud


def _era5_100m(config: Config, point_id: str) -> pd.Series | None:
    """100 m wind for one ERA5 point, read from the DB (era5_point_hourly)."""
    from ..io.era5 import read_era5_point
    return read_era5_point(config, point_id, "wind100_ms")


def _national_pv(weather: pd.DataFrame, stations: pd.DataFrame) -> pd.Series:
    """Equal-weighted national PV CF (default representative cohort)."""
    cohort = PVCohort()
    meta = stations.set_index(stations["station_id"].astype(str))
    acc, cnt = None, 0
    for sid, g in weather.groupby(weather["station_id"].astype(str)):
        if sid not in meta.index:
            continue
        g = g.sort_values("timestamp_utc")
        ts = pd.DatetimeIndex(g["timestamp_utc"])
        r = meta.loc[sid]
        alt = float(r["altitude"]) if pd.notna(r.get("altitude")) else 0.0
        ghi = ghi_from_cloud(ts, float(r["latitude"]), float(r["longitude"]),
                             g["cloud_cover_pct"].to_numpy(), alt).to_numpy()
        cf = pv_cf(ts, float(r["latitude"]), float(r["longitude"]), ghi,
                   g["temperature_c"].to_numpy(), cohort, alt)
        s = pd.Series(cf.to_numpy(), index=ts)
        acc = s if acc is None else acc.add(s, fill_value=0.0)
        cnt += 1
    return (acc / max(cnt, 1)).rename("pv")


def _era5_100m_national(config: Config, stations: pd.DataFrame) -> pd.Series:
    cols = [s for s in (_era5_100m(config, sid) for sid in stations["station_id"].astype(str))
            if s is not None]
    return pd.concat(cols, axis=1).mean(axis=1)


def _offshore_national(config: Config) -> pd.Series | None:
    """Capacity-weighted national offshore CF with a **time-varying fleet**: each farm only contributes
    once commissioned (weighted by its capacity). Fixes the historical mismatch where future / non-FR-
    Atlantic farms polluted the modelled aggregate."""
    wb = config.resolve(config.section("assumptions")["workbook"])
    if not (wb and wb.exists()):
        return None
    from powersim_core.scenario import load_sheet
    farms = load_sheet(wb, "res", "offshore_farms")
    num, den = None, None
    for _, fr in farms.iterrows():
        w = _era5_100m(config, f"farm_{fr['farm']}")
        if w is None:
            continue
        active = pd.Series((w.index.year >= int(fr["commissioning_year"])).astype(float), index=w.index)
        cap_t = active * float(fr["capacity_mw"])                 # 0 before commissioning (Series)
        cf = offshore_farm_cf(w) * cap_t
        num = cf if num is None else num.add(cf, fill_value=0.0)
        den = cap_t if den is None else den.add(cap_t, fill_value=0.0)
    if num is None:
        return None
    out = (num / den.replace(0.0, np.nan)).rename("wind_offshore")
    return out


def _build(config: Config) -> tuple[pd.DataFrame, pd.DataFrame]:
    from ..io.loaders import load_weather_hist
    weather, stations = load_weather_hist(config)
    pv = _national_pv(weather, stations)
    w100 = _era5_100m_national(config, stations)
    precip = weather.groupby("timestamp_utc")["precip_1h_mm"].mean()
    onshore = onshore_cf(w100).rename("wind_onshore")
    hydro = ror_cf(precip).rename("hydro_ror")
    parts = [pv, onshore, hydro]
    off = _offshore_national(config)
    if off is not None:
        parts.append(off)
    cf = pd.concat(parts, axis=1); cf.index.name = "timestamp_utc"
    temp = weather.groupby("timestamp_utc")["temperature_c"].mean()
    drivers = pd.concat([w100.rename("w100_nat"), precip.rename("precip_nat"),
                         temp.rename("temp_nat")], axis=1)
    drivers.index.name = "timestamp_utc"
    return cf, drivers


def modelled_hist_cf(config: Config, force: bool = False) -> pd.DataFrame:
    """Uncalibrated modelled national CF per technology over history (cached, with drivers)."""
    if lake.exists("res", "hist_modelled_cf") and lake.exists("res", "hist_drivers") and not force:
        return lake.read_table("res", "hist_modelled_cf")
    cf, drivers = _build(config)
    lake.write_table(cf, "res", "hist_modelled_cf")
    lake.write_table(drivers, "res", "hist_drivers")
    return cf


def hist_drivers(config: Config) -> pd.DataFrame:
    """National 100 m wind + precip drivers (built alongside modelled_hist_cf)."""
    if not lake.exists("res", "hist_drivers"):
        modelled_hist_cf(config)
    return lake.read_table("res", "hist_drivers")
