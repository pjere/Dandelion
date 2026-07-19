"""ERA5 download via the Copernicus CDS API (reanalysis-era5-single-levels).

Chunked by year, cached to ``data/era5/cache/`` (re-runs skip existing years), then
read back and collocated to stations by :func:`weathergen.io._era5_to_station_cube`.
Credentials come from ``~/.cdsapirc`` (never stored in the repo).
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import xarray as xr

from .config import Config

# config short name -> CDS API long variable name
CDS_LONG = {
    "2t": "2m_temperature",
    "2d": "2m_dewpoint_temperature",
    "msl": "mean_sea_level_pressure",
    "10u": "10m_u_component_of_wind",
    "10v": "10m_v_component_of_wind",
    "tcc": "total_cloud_cover",
    "lcc": "low_cloud_cover",
    "tp": "total_precipitation",
}
_ALL_DAYS = [f"{d:02d}" for d in range(1, 32)]
_ALL_HOURS = [f"{h:02d}:00" for h in range(24)]


def build_request(config: Config, year: int, months: list[int] | None = None) -> tuple[str, dict]:
    """Build the (dataset, request) pair for one year (optionally a month subset)."""
    e = config.section("data")["era5"]
    bbox = config.section("data")["station_filter"]["bbox"]
    variables = sorted({CDS_LONG[s] for s in e["variable_map"] if s in CDS_LONG})
    months = months or list(range(1, 13))
    request = {
        "product_type": ["reanalysis"],
        "variable": variables,
        "year": [str(year)],
        "month": [f"{m:02d}" for m in months],
        "day": _ALL_DAYS,
        "time": _ALL_HOURS,
        # area = [North, West, South, East]
        "area": [bbox["lat_max"], bbox["lon_min"], bbox["lat_min"], bbox["lon_max"]],
        "grid": [0.25, 0.25],
        "data_format": "netcdf",
        "download_format": "unarchived",
    }
    return e["cds"].get("dataset", "reanalysis-era5-single-levels"), request


def month_list(start_year: int, end_year: int) -> list[tuple[int, int]]:
    return [(y, m) for y in range(start_year, end_year + 1) for m in range(1, 13)]


def download_months(config: Config, ym: list[tuple[int, int]]) -> list[Path]:
    """Download (with cache) one NetCDF per (year, month) — small enough for CDS cost limits.

    Cached/resumable: existing month files are skipped. Returns the local file list.
    """
    import cdsapi

    e = config.section("data")["era5"]
    cache = config.resolve(e["path"]).parent / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    client = cdsapi.Client()
    files: list[Path] = []
    for y, m in ym:
        target = cache / f"era5_{y}{m:02d}.nc"
        if not target.exists() or target.stat().st_size == 0:
            dataset, request = build_request(config, y, [m])
            try:
                client.retrieve(dataset, request).download(str(target))
            except Exception as exc:  # noqa: BLE001 - surface actionable CDS messages
                msg = str(exc).lower()
                if "licence" in msg:
                    raise RuntimeError(
                        "ERA5 licence not accepted: accept it once at "
                        "https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels"
                        "?tab=download#manage-licences"
                    ) from exc
                raise
        files.append(target)
    return files


def _normalize(ds: xr.Dataset) -> xr.Dataset:
    if "valid_time" in ds.coords and "time" not in ds.coords:
        ds = ds.rename({"valid_time": "time"})
    for extra in ("number", "expver"):
        if extra in ds.variables and extra not in ds.dims:
            ds = ds.drop_vars(extra, errors="ignore")
    return ds


def _open_one(path: Path) -> xr.Dataset:
    """Open one downloaded file; the new CDS may deliver a ZIP with instant + accumulated
    NetCDFs split apart — extract and merge them into a single dataset."""
    if zipfile.is_zipfile(path):
        dest = path.parent / (path.stem + "_unzip")
        dest.mkdir(exist_ok=True)
        with zipfile.ZipFile(path) as z:
            z.extractall(dest)
        members = [_normalize(xr.open_dataset(str(n))) for n in sorted(dest.glob("*.nc"))]
        return xr.merge(members, compat="override", join="outer")
    return _normalize(xr.open_dataset(str(path)))


def open_era5(files: list[Path]) -> xr.Dataset:
    """Open + concatenate downloaded ERA5 month files (time-sorted). Dask-free (xr.concat)."""
    parts = [_open_one(f) for f in files]
    ds = parts[0] if len(parts) == 1 else xr.concat(parts, dim="time")
    return ds.sortby("time")
