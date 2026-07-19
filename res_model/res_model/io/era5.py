"""Phase 1 — ERA5 extraction for the wind bridge (§2.A) and PV cross-check.

Pulls 100 m wind (u100, v100) and surface solar radiation downwards (ssrd) at arbitrary points
(stations + offshore farm locations) via the CDS ARCO point time-series dataset — one small request
per point for the full hourly record, cached/resumable (same fast-path as weathergen's era5_arco).
The station-10 m → ERA5-100 m transfer (D1) is estimated on this in Phase 2.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import Config

DATASET = "reanalysis-era5-single-levels-timeseries"
ARCO_LONG = ["100m_u_component_of_wind", "100m_v_component_of_wind",
             "surface_solar_radiation_downwards"]
ERA5_TABLE = "era5_point_hourly"          # DB table holding the ingested ERA5 point series


def _cache(config: Config) -> Path:
    d = config.resolve(config.section("era5")["cache_dir"])
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_points(config: Config) -> pd.DataFrame:
    """The extraction point set: métropole stations (station_id) + offshore farms (farm_<name>)."""
    import sqlite3
    d = config.section("data")
    b = config.section("weather")["bbox"]
    con = sqlite3.connect(config.resolve(d["sqlite_path"]))
    try:
        st = pd.read_sql(f'SELECT station_id, latitude, longitude FROM {d["registry"]["station_table"]}', con)
    finally:
        con.close()
    st = st[st.latitude.between(b["lat_min"], b["lat_max"]) &
            st.longitude.between(b["lon_min"], b["lon_max"])].dropna(subset=["latitude", "longitude"])
    stations = pd.DataFrame({"point_id": st["station_id"].astype(str),
                             "latitude": st["latitude"].astype(float),
                             "longitude": st["longitude"].astype(float)})
    farms = pd.DataFrame(columns=["point_id", "latitude", "longitude"])
    wb = config.resolve(config.section("assumptions")["workbook"])
    if wb and Path(wb).exists():
        from powersim_core.scenario import load_sheet
        of = load_sheet(wb, "res", "offshore_farms")
        farms = pd.DataFrame({"point_id": "farm_" + of["farm"].astype(str),
                              "latitude": of["latitude"].astype(float),
                              "longitude": of["longitude"].astype(float)})
    return pd.concat([stations, farms], ignore_index=True).drop_duplicates("point_id")


def download_points(config: Config, points: pd.DataFrame) -> dict[str, Path]:
    """One ARCO request per point (columns: point_id, latitude, longitude). Cached/resumable."""
    import cdsapi

    p = config.section("data")["period"]
    cache = _cache(config)
    client = cdsapi.Client()
    out: dict[str, Path] = {}
    for _, row in points.iterrows():
        pid = str(row["point_id"])
        target = cache / f"era5_{pid}.zip"
        if not target.exists() or target.stat().st_size == 0:
            req = {"variable": ARCO_LONG,
                   "location": {"latitude": float(row["latitude"]), "longitude": float(row["longitude"])},
                   "date": [f"{p['start']}/{p['end']}"], "data_format": "netcdf"}
            client.retrieve(DATASET, req).download(str(target))
            print(f"[era5] {pid} ok", flush=True)
        out[pid] = target
    return out


def _open(path: Path):
    import xarray as xr
    dest = path.parent / (path.stem + "_x"); dest.mkdir(exist_ok=True)
    with zipfile.ZipFile(path) as z:
        z.extractall(dest)
    ds = xr.open_dataset(str(sorted(dest.glob("*.nc"))[0]))
    if "valid_time" in ds.coords and "time" not in ds.coords:
        ds = ds.rename({"valid_time": "time"})
    return ds


def derive(ds) -> pd.DataFrame:
    """ERA5 point dataset → hourly (wind100_ms, ghi_wm2). Pure; unit-testable without network.

    ``ssrd`` is accumulated J·m⁻² over the hour → divide by 3600 for mean W·m⁻²."""
    time = pd.to_datetime(ds["time"].values)
    if time.tz is None:
        time = time.tz_localize("UTC")
    u = np.asarray(ds["u100"].values, float).ravel()
    v = np.asarray(ds["v100"].values, float).ravel()
    ssrd = np.asarray(ds["ssrd"].values, float).ravel()
    return pd.DataFrame({
        "timestamp_utc": time,
        "wind100_ms": np.sqrt(u ** 2 + v ** 2),
        "ghi_wm2": np.clip(ssrd / 3600.0, 0.0, None),
    })


def load_era5_points(config: Config, points: pd.DataFrame) -> pd.DataFrame:
    """Tidy per-point ERA5 (timestamp_utc, point_id, wind100_ms, ghi_wm2). Triggers downloads."""
    files = download_points(config, points)
    frames = []
    for pid, path in files.items():
        df = derive(_open(path))
        df["point_id"] = pid
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


# --------------------------------------------------------------------------- #
#  DB ingest + reader — makes the ERA5 100 m wind / SSRD a first-class DB source
#  (so every weather variable in the predictive model comes from the DB, like SYNOP).
# --------------------------------------------------------------------------- #
def _db(config: Config):
    import sqlite3
    return sqlite3.connect(config.resolve(config.section("data")["sqlite_path"]))


def ingest_to_db(config: Config) -> int:
    """Load every cached ERA5 point (era5_cache/*.zip) into the ``era5_point_hourly`` DB table.
    Idempotent (INSERT OR REPLACE on (point_id, ts_utc)). The zips remain a raw download cache."""
    cache = config.resolve(config.section("era5")["cache_dir"])
    con = _db(config)
    try:
        con.execute(f'CREATE TABLE IF NOT EXISTS "{ERA5_TABLE}" ('
                    'point_id TEXT, ts_utc TEXT, wind100_ms REAL, ghi_wm2 REAL, '
                    'PRIMARY KEY (point_id, ts_utc))')
        total = 0
        for f in sorted(cache.glob("era5_*.zip")):
            pid = f.stem.replace("era5_", "")
            df = derive(_open(f))
            ts = pd.DatetimeIndex(df["timestamp_utc"]).strftime("%Y-%m-%dT%H:%M:%S+00:00")
            recs = list(zip([pid] * len(df), ts, df["wind100_ms"].astype(float),
                            df["ghi_wm2"].astype(float)))
            con.executemany(f'INSERT OR REPLACE INTO "{ERA5_TABLE}" VALUES (?,?,?,?)', recs)
            con.commit()
            total += len(recs)
            print(f"[era5-ingest] {pid} -> {len(recs)} rows", flush=True)
        return total
    finally:
        con.close()


def read_era5_point(config: Config, point_id: str, var: str = "wind100_ms") -> pd.Series | None:
    """Read one ERA5 point's series (default 100 m wind) from the DB. Returns None if absent."""
    con = _db(config)
    try:
        have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if ERA5_TABLE not in have:
            return None
        df = pd.read_sql(f'SELECT ts_utc, {var} FROM "{ERA5_TABLE}" WHERE point_id = ?',
                         con, params=[str(point_id)])
    finally:
        con.close()
    if df.empty:
        return None
    idx = pd.to_datetime(df["ts_utc"], utc=True)
    return pd.Series(df[var].to_numpy(), index=idx, name=str(point_id)).sort_index()
