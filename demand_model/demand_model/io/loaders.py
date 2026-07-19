"""Phase 1 — historical loaders from the pricemodeling SQLite DB.

Demand = RTE REALISED consommation − pumping (pumping = -min(HYDRO_PUMPED_STORAGE, 0)), 15-min
aggregated to hourly, tz-aware UTC. Weather = per-station hourly (temperature, wind, cloud,
humidity). All outputs pass their pandera contract.
"""
from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

from ..config import Config
from .schemas import LOAD_HIST, WEATHER, validate


def _con(config: Config) -> sqlite3.Connection:
    return sqlite3.connect(config.resolve(config.section("data")["sqlite_path"]))


def _to_hourly_utc(ts: pd.Series, val: pd.Series, how: str = "mean") -> pd.Series:
    """Floor tz-aware UTC timestamps to the hour and aggregate (mean over the 15-min steps)."""
    t = pd.to_datetime(ts, utc=True).dt.floor("h")
    s = pd.Series(pd.to_numeric(val, errors="coerce").to_numpy(), index=t)
    return s.groupby(level=0).agg(how)


def load_demand(config: Config) -> pd.DataFrame:
    """REALISED − pumping, hourly UTC -> LOAD_HIST contract."""
    d = config.section("data")["load"]
    p = config.section("data")["period"]
    con = _con(config)
    try:
        realised = pd.read_sql(
            f'SELECT ts_utc, value FROM "{d["table"]}" '
            f"WHERE series_key = ? AND ts_utc >= ? AND ts_utc <= ? AND value IS NOT NULL",
            con, params=[d["series"], p["start"], p["end"]],
        )
        pump = pd.read_sql(
            f'SELECT ts_utc, value FROM "{d["pumping_series_table"]}" '
            f"WHERE series_key = ? AND ts_utc >= ? AND ts_utc <= ? AND value IS NOT NULL",
            con, params=[d["pumping_series_key"], p["start"], p["end"]],
        )
    finally:
        con.close()

    realised_h = _to_hourly_utc(realised["ts_utc"], realised["value"])
    demand = realised_h.rename("load_mw")
    if config.section("perimeter").get("subtract_pumping", True) and not pump.empty:
        pump_load = pd.Series(np.clip(-pd.to_numeric(pump["value"], errors="coerce"), 0, None).to_numpy(),
                              index=pd.to_datetime(pump["ts_utc"], utc=True).dt.floor("h"))
        pump_h = pump_load.groupby(level=0).mean().reindex(demand.index).fillna(0.0)
        demand = demand - pump_h

    out = demand.reset_index(); out.columns = ["timestamp_utc", "load_mw"]
    out = out.dropna(subset=["load_mw"]).sort_values("timestamp_utc").reset_index(drop=True)
    return validate(out, LOAD_HIST, "load")


def _metropole_stations(con: sqlite3.Connection, config: Config) -> pd.DataFrame:
    d = config.section("data")
    m = pd.read_sql(f'SELECT station_id, latitude, longitude, altitude FROM {d["weather"]["station_table"]}', con)
    b = d["weather"]["bbox"]
    return m[m.latitude.between(b["lat_min"], b["lat_max"]) & m.longitude.between(b["lon_min"], b["lon_max"])] \
        .sort_values("station_id").reset_index(drop=True)


WEATHER_VARS = ["temperature_c", "wind_speed_ms", "cloud_cover_pct", "humidity_pct"]


def load_weather(config: Config) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per-station hourly weather (tidy long) + station metadata. Returns (weather_df, stations)."""
    d = config.section("data")
    p = d["period"]
    con = _con(config)
    try:
        stations = _metropole_stations(con, config)
        cols = [f"meteo_{s}_{v}" for s in stations.station_id for v in WEATHER_VARS]
        have = {r[1] for r in con.execute(f'PRAGMA table_info("{d["weather"]["master_table"]}")').fetchall()}
        sel = [c for c in cols if c in have]
        df = pd.read_sql(
            f'SELECT ts_utc, {", ".join(sel)} FROM {d["weather"]["master_table"]} '
            f"WHERE ts_utc >= ? AND ts_utc <= ? ORDER BY ts_utc",
            con, params=[p["start"], p["end"]],
        )
    finally:
        con.close()

    time = pd.to_datetime(df["ts_utc"], utc=True)          # tz-aware UTC (kept through concat)
    frames = []
    for sid in stations.station_id:
        block = pd.DataFrame({"timestamp_utc": time.values, "station_id": str(sid)})
        block["timestamp_utc"] = pd.to_datetime(block["timestamp_utc"], utc=True)
        for v in WEATHER_VARS:
            col = f"meteo_{sid}_{v}"
            block[v] = pd.to_numeric(df[col], errors="coerce").to_numpy() if col in df.columns else np.nan
        frames.append(block)
    tidy = pd.concat(frames, ignore_index=True)
    tidy["timestamp_utc"] = pd.to_datetime(tidy["timestamp_utc"], utc=True)
    for b in ("cloud_cover_pct", "humidity_pct"):          # clip upstream rounding artifacts (e.g. 101%)
        if b in tidy:
            tidy[b] = tidy[b].clip(0, 100)
    validate(tidy.head(20000), WEATHER, "weather")         # contract on a sample (full frame is millions of rows)
    return tidy, stations
