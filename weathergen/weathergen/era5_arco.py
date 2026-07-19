"""Fast ERA5 ingestion via the CDS ARCO point time-series dataset.

`reanalysis-era5-single-levels-timeseries` returns the FULL hourly record (1940→present) for a
single point in one small request. So the whole station panel is ~one request per station
(~42) instead of the ~576 gridded monthly chunks — hours instead of days, and cached/resumable
per station.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from .config import Config

DATASET = "reanalysis-era5-single-levels-timeseries"
# CDS long names of the fields we need (humidity is derived from t + dewpoint)
ARCO_LONG = [
    "2m_temperature", "2m_dewpoint_temperature", "mean_sea_level_pressure",
    "10m_u_component_of_wind", "10m_v_component_of_wind",
    "total_cloud_cover", "total_precipitation",
]
# netcdf short name -> internal "raw" key consumed by weathergen.io._era5_variable
NC_TO_RAW = {
    "t2m": "temperature_c", "d2m": "dew_point_c", "msl": "pressure_sea_hpa",
    "u10": "_u_wind", "v10": "_v_wind", "tcc": "cloud_cover_pct", "tp": "precip_1h_mm",
}


def _cache(config: Config) -> Path:
    d = config.resolve(config.section("data")["era5"]["path"]).parent / "arco"
    d.mkdir(parents=True, exist_ok=True)
    return d


def download_stations(config: Config, meta: pd.DataFrame) -> dict[str, Path]:
    """One request per station for the full record (cached/resumable). Returns {station_id: file}."""
    import cdsapi

    e = config.section("data")["era5"]
    start = str(e.get("start", "1979-01-01"))
    end = str(config.section("data")["period"]["end"])
    cache = _cache(config)
    client = cdsapi.Client()
    out: dict[str, Path] = {}
    for _, row in meta.iterrows():
        sid = str(row["station_id"])
        target = cache / f"arco_{sid}.zip"
        if not target.exists() or target.stat().st_size == 0:
            req = {
                "variable": ARCO_LONG,
                "location": {"latitude": float(row["latitude"]), "longitude": float(row["longitude"])},
                "date": [f"{start}/{end}"],
                "data_format": "netcdf",
            }
            client.retrieve(DATASET, req).download(str(target))
            print(f"[arco] {sid} ok", flush=True)
        out[sid] = target
    return out


def _open_station(path: Path) -> xr.Dataset:
    dest = path.parent / (path.stem + "_x")
    dest.mkdir(exist_ok=True)
    with zipfile.ZipFile(path) as z:
        z.extractall(dest)
    ds = xr.open_dataset(str(sorted(dest.glob("*.nc"))[0]))
    if "valid_time" in ds.coords and "time" not in ds.coords:
        ds = ds.rename({"valid_time": "time"})
    return ds


def load_arco_cube(config: Config, meta: pd.DataFrame) -> xr.DataArray:
    """Build the (time, station, variable) ERA5 cube from per-station ARCO time-series."""
    from .io import _empty_cube, _era5_variable

    files = download_stations(config, meta)
    var_names = config.var_names
    per_station: dict[str, xr.Dataset] = {sid: _open_station(p) for sid, p in files.items()}
    time = pd.to_datetime(next(iter(per_station.values()))["time"].values)
    cube = _empty_cube(time.to_numpy(), meta, var_names)

    for si, sid in enumerate(meta.station_id.astype(str)):
        ds = per_station[sid].reindex(time=time.to_numpy())
        raw = {NC_TO_RAW[k]: np.asarray(ds[k].values, dtype="float64")
               for k in NC_TO_RAW if k in ds}
        for vi, v in enumerate(var_names):
            cube.values[:, si, vi] = _era5_variable(v, raw)
    cube.attrs["source"] = "era5_arco"
    return cube
