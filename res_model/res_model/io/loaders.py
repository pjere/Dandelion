"""Phase 1 — historical loaders from the pricemodeling SQLite DB + the weathergen cube.

Production = national RTE per-type (SOLAR / WIND_ONSHORE / WIND_OFFSHORE / HYDRO_ROR), normalised by
the time-varying installed capacity to a capacity factor. Offshore is additionally available
farm-level from per-unit. Weather uses the SAME interface for history (DB) and synthetic draws (cube),
producing the tidy WEATHER contract.
"""
from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

from ..config import Config
from .schemas import PRODUCTION_HIST, REGISTRY, WEATHER, validate

# station weather variables res_model needs (temperature, 10 m wind, cloud→GHI, precip→hydro)
WEATHER_VARS = ["temperature_c", "wind_speed_ms", "cloud_cover_pct", "precip_1h_mm"]
_OFFSHORE_TOKENS = ("EOLIEN EN MER", "OFFSHORE", "FECAMP", "NAZAIRE", "BRIEUC",
                    "COURSEULLES", "TREPORT", "YEU", "DUNKERQUE")


def _con(config: Config) -> sqlite3.Connection:
    return sqlite3.connect(config.resolve(config.section("data")["sqlite_path"]))


# --------------------------------------------------------------------------- #
#  Production + capacity + capacity factor
# --------------------------------------------------------------------------- #
def load_production(config: Config) -> pd.DataFrame:
    """National hourly production for the four technologies → PRODUCTION_HIST (region 'FR')."""
    d = config.section("data")
    keys = d["production"]["series_keys"]
    p = d["period"]
    con = _con(config)
    try:
        frames = []
        for tech, key in keys.items():
            df = pd.read_sql(
                f'SELECT ts_utc, value FROM "{d["production"]["per_type_table"]}" '
                f"WHERE series_key = ? AND ts_utc >= ? AND ts_utc <= ? AND value IS NOT NULL",
                con, params=[key, p["start"], p["end"]])
            df["timestamp_utc"] = pd.to_datetime(df["ts_utc"], utc=True)
            df["technology"] = tech
            df["region"] = "FR"
            df = df.rename(columns={"value": "production_mw"})[
                ["timestamp_utc", "technology", "region", "production_mw"]]
            # small negative night-time readings (inverter parasitic draw / metering noise) → 0;
            # we model potential production ≥ 0
            df["production_mw"] = df["production_mw"].clip(lower=0.0)
            frames.append(df)
    finally:
        con.close()
    out = pd.concat(frames, ignore_index=True).sort_values(["technology", "timestamp_utc"])
    return validate(out.reset_index(drop=True), PRODUCTION_HIST, "production")


def _capacity_series(config: Config, key: str) -> pd.DataFrame:
    d = config.section("data")
    con = _con(config)
    try:
        c = pd.read_sql(
            f'SELECT ts_utc, value FROM "{d["production"]["capacity_type_table"]}" '
            f"WHERE series_key = ? AND value IS NOT NULL", con, params=[key])
    finally:
        con.close()
    c["t"] = pd.to_datetime(c["ts_utc"], utc=True)
    return c.drop_duplicates("t").sort_values("t")[["t", "value"]].rename(columns={"value": "capacity_mw"})


def capacity_factor(config: Config) -> pd.DataFrame:
    """Per-technology hourly CF = production / (time-varying installed capacity)."""
    keys = config.section("data")["production"]["series_keys"]
    prod = load_production(config)
    out = []
    for tech, key in keys.items():
        p = prod[prod["technology"] == tech].sort_values("timestamp_utc").copy()
        cap = _capacity_series(config, key)
        merged = pd.merge_asof(p, cap.rename(columns={"t": "timestamp_utc"}),
                               on="timestamp_utc", direction="backward")
        merged["cf"] = merged["production_mw"] / merged["capacity_mw"]
        out.append(merged[["timestamp_utc", "technology", "region", "production_mw",
                           "capacity_mw", "cf"]])
    return pd.concat(out, ignore_index=True)


def load_offshore_units(config: Config) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Farm-level offshore production from per-unit + a registry frame (best-effort match by label)."""
    d = config.section("data")
    con = _con(config)
    try:
        reg = pd.read_sql(f'SELECT eic_code, canonical_name, fuel_type, first_seen, last_seen '
                          f'FROM "{d["registry"]["unit_table"]}"', con)
        like = " OR ".join([f"UPPER(label) LIKE '%{t}%'" for t in _OFFSHORE_TOKENS])
        u = pd.read_sql(
            f'SELECT ts_utc, series_key, label, value FROM "{d["production"]["per_unit_table"]}" '
            f"WHERE ({like}) AND value IS NOT NULL", con)
    finally:
        con.close()
    if u.empty:
        return u, reg.head(0)
    u["timestamp_utc"] = pd.to_datetime(u["ts_utc"], utc=True)
    u = u.rename(columns={"value": "production_mw", "series_key": "unit_id"})
    u["technology"] = "wind_offshore"
    registry = (u.groupby(["unit_id", "label"]).size().reset_index()
                .rename(columns={"label": "canonical_name", 0: "n"}))
    registry["technology"] = "wind_offshore"
    registry = registry.rename(columns={"unit_id": "unit_id"})
    registry = validate(registry[["unit_id", "technology"]].assign(region="FR"), REGISTRY, "registry")
    return u[["timestamp_utc", "unit_id", "label", "technology", "production_mw"]], registry


# --------------------------------------------------------------------------- #
#  Weather — one interface for history (DB) and synthetic draws (cube)
# --------------------------------------------------------------------------- #
def _metropole_stations(con: sqlite3.Connection, config: Config) -> pd.DataFrame:
    d = config.section("data")
    m = pd.read_sql(f'SELECT station_id, latitude, longitude, altitude, region '
                    f'FROM {d["registry"]["station_table"]}', con)
    b = config.section("weather")["bbox"]
    return (m[m.latitude.between(b["lat_min"], b["lat_max"]) &
              m.longitude.between(b["lon_min"], b["lon_max"])]
            .sort_values("station_id").reset_index(drop=True))


def load_weather_hist(config: Config) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per-station historical weather (tidy WEATHER) + station metadata (with region)."""
    d = config.section("data")
    p = d["period"]
    con = _con(config)
    try:
        stations = _metropole_stations(con, config)
        cols = [f"meteo_{s}_{v}" for s in stations.station_id for v in WEATHER_VARS]
        have = {r[1] for r in con.execute('PRAGMA table_info("master_hourly")').fetchall()}
        sel = [c for c in cols if c in have]
        df = pd.read_sql(
            f'SELECT ts_utc, {", ".join(sel)} FROM master_hourly '
            f"WHERE ts_utc >= ? AND ts_utc <= ? ORDER BY ts_utc", con, params=[p["start"], p["end"]])
    finally:
        con.close()
    time = pd.to_datetime(df["ts_utc"], utc=True)
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
    if "cloud_cover_pct" in tidy:
        tidy["cloud_cover_pct"] = tidy["cloud_cover_pct"].clip(0, 100)
    for v in ("wind_speed_ms", "precip_1h_mm"):               # tiny negatives are rounding artifacts
        if v in tidy:
            tidy[v] = tidy[v].clip(lower=0)
    validate(tidy.head(20000), WEATHER, "weather")            # contract on a sample (full frame is huge)
    return tidy, stations


_ENSEMBLE_DIMS = ("realization", "member", "draw", "ensemble")


def load_weather_synthetic(config: Config, realization: int = 0) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per-station synthetic weather (tidy WEATHER) for one draw of the weathergen cube — the SAME
    cube the demand model consumes, so demand↔RES draws are identical.

    Delegates to the shared powersim_core cube reader; the RES-specific steps are preferring the cube's
    co-generated 100 m wind when present (weathergen Option B) and clipping cloud cover to [0, 100]."""
    from powersim_core.weather_cube import cube_variables, load_station_tidy
    path = config.resolve(config.section("weather")["weathergen_output"])
    syn_vars = WEATHER_VARS + (["wind_speed_100m_ms"] if "wind_speed_100m_ms" in cube_variables(path) else [])
    tidy, stations = load_station_tidy(path, syn_vars, realization)
    if "cloud_cover_pct" in tidy:
        tidy["cloud_cover_pct"] = tidy["cloud_cover_pct"].clip(0, 100)
    return tidy, stations
