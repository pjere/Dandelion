"""Phase 6 — national projection drivers from the weathergen cube (coherent with the demand model).

Reads the SAME cube the demand model consumes (one realization = one coherent weather draw), so the
demand↔RES correlation is preserved by construction. Produces the per-technology deterministic drivers:
  pv_raw   — national raw PV CF (physical chain on cloud→GHI + temperature; = calibration chain)
  w100_nat — national mean 100 m wind (weathergen co-generated `wind_speed_100m_ms`)
  offshore_wind — national 100 m wind × (offshore/station 100 m ratio from ERA5 in the DB)
  precip_nat, temp_nat — for the hydro blend
Cached per realization.
"""
from __future__ import annotations

import pandas as pd

from powersim_core import lake

from ..config import Config


def _offshore_ratio(config: Config) -> float:
    """Mean offshore-farm 100 m wind / mean station 100 m wind, from the ERA5 DB table."""
    import sqlite3

    from ..io.era5 import ERA5_TABLE
    con = sqlite3.connect(config.resolve(config.section("data")["sqlite_path"]))
    try:
        have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if ERA5_TABLE not in have:
            return 1.25
        farm = pd.read_sql(f'SELECT AVG(wind100_ms) v FROM "{ERA5_TABLE}" '
                           "WHERE point_id LIKE 'farm_%'", con)["v"][0]
        stn = pd.read_sql(f'SELECT AVG(wind100_ms) v FROM "{ERA5_TABLE}" '
                          "WHERE point_id NOT LIKE 'farm_%'", con)["v"][0]
    finally:
        con.close()
    return float(farm / stn) if stn and farm else 1.25


def projection_drivers(config: Config, realization: int = 0, force: bool = False) -> pd.DataFrame:
    """National deterministic drivers over the projection horizon for one weather realization."""
    from ..calibration.historical import _national_pv
    from ..io.loaders import load_weather_synthetic
    cache = lake.table_path("res", "proj_drivers", realization=realization)
    cube_path = config.resolve(config.section("weather")["weathergen_output"])
    fresh = (cache.exists() and cube_path and cube_path.exists()
             and cache.stat().st_mtime >= cube_path.stat().st_mtime)
    if fresh and not force:                          # invalidate if the cube was regenerated
        return lake.read_table("res", "proj_drivers", realization=realization)
    weather, stations = load_weather_synthetic(config, realization)
    if "wind_speed_100m_ms" not in weather:
        raise ValueError("cube has no 'wind_speed_100m_ms' — regenerate weathergen with wind100")
    pv_raw = _national_pv(weather, stations).rename("pv_raw")
    g = weather.groupby("timestamp_utc")
    w100 = g["wind_speed_100m_ms"].mean().rename("w100_nat")
    precip = g["precip_1h_mm"].mean().rename("precip_nat")
    temp = g["temperature_c"].mean().rename("temp_nat")
    offshore = (w100 * _offshore_ratio(config)).rename("offshore_wind")
    df = pd.concat([pv_raw, w100, offshore, precip, temp], axis=1)
    df.index.name = "timestamp_utc"
    lake.write_table(df, "res", "proj_drivers", realization=realization)
    return df
