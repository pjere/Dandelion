"""Fetch CMIP6 projections (CDS 'projections-cmip6') and compute per-month quantile deltas.

The SSP scenario and the horizon (target year) are *inputs* to the tool. For the chosen SSP we
download DAILY data over the France box for a historical baseline window and a future window
centred on the target year, then compute per-variable, per-month quantile deltas
(future quantiles vs historical quantiles) — additive for temperature/dew point, multiplicative
for wind/precip. Because the deltas are quantile-wise from daily data, tail intensification is
captured directly. Saved as the npz format weathergen.trend expects.

Small download: area-subset to France (a few grid points) makes this a handful of CDS requests.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

from .config import Config

DATASET = "projections-cmip6"
DEFAULT_MODEL = "mpi_esm1_2_lr"     # EC-Earth3 has broken roocs subsetting on CDS; MPI works
BASELINE = (1995, 2014)                    # standard IPCC present-day baseline (historical run)

# our variable -> (CDS variable name, netcdf short name, application mode)
CMIP6_VARS = {
    "temperature_c":   ("near_surface_air_temperature", "tas", "add"),
    "wind_speed_ms":   ("near_surface_wind_speed", "sfcWind", "mult"),
    "precip_1h_mm":    ("precipitation", "pr", "mult"),
    "cloud_cover_pct": ("total_cloud_cover_percentage", "clt", "add"),
}
SSP_MAP = {
    "ssp126": "ssp1_2_6", "ssp245": "ssp2_4_5", "ssp370": "ssp3_7_0", "ssp585": "ssp5_8_5",
}
QUANTILES = np.linspace(0.05, 0.95, 19)


def future_window(target_year: int) -> tuple[int, int]:
    return target_year - 9, target_year + 10        # 20-yr window centred ~target


def _cache(config: Config) -> Path:
    d = config.resolve(config.section("data")["era5"]["path"]).parent.parent / "cmip6" / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _build_request(experiment: str, cds_var: str, years: list[int], area: list[float],
                   model: str) -> dict:
    return {
        "temporal_resolution": "daily",
        "experiment": experiment,
        "variable": cds_var,
        "model": model,
        "year": [str(y) for y in years],
        "month": [f"{m:02d}" for m in range(1, 13)],
        "day": [f"{d:02d}" for d in range(1, 32)],
        "area": area,                                # [N, W, S, E]
        "format": "zip",
    }


def download(config: Config, ssp: str, target_year: int, model: str = DEFAULT_MODEL) -> dict[str, dict[str, Path]]:
    """Download (cached) historical + SSP daily files per variable. Returns {var: {period: file}}."""
    import cdsapi

    experiment = SSP_MAP.get(ssp, ssp)
    bbox = config.section("data")["station_filter"]["bbox"]
    area = [bbox["lat_max"], bbox["lon_min"], bbox["lat_min"], bbox["lon_max"]]
    cache = _cache(config)
    client = cdsapi.Client()
    fut = future_window(target_year)
    windows = {"hist": ("historical", list(range(*(BASELINE[0], BASELINE[1] + 1)))),
               "fut": (experiment, list(range(fut[0], fut[1] + 1)))}
    out: dict[str, dict[str, Path]] = {}
    for our_var, (cds_var, _short, _mode) in CMIP6_VARS.items():
        got = {}
        for period, (exp, years) in windows.items():
            tag = f"{model}_{exp}_{cds_var}_{years[0]}-{years[-1]}"
            target = cache / f"{tag}.zip"
            try:
                if not target.exists() or target.stat().st_size == 0:
                    client.retrieve(DATASET, _build_request(exp, cds_var, years, area, model)).download(str(target))
                got[period] = target
                print(f"[cmip6] ok  {our_var:16s} {period:4s} {exp}", flush=True)
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "403" in msg or "licence" in msg.lower() or "forbidden" in msg.lower():
                    raise RuntimeError(
                        "CMIP6 licence not accepted on your CDS account. Accept it once at "
                        "https://cds.climate.copernicus.eu/datasets/projections-cmip6?tab=download "
                        "(scroll to 'Terms of use' / manage licences), then re-run fetch-cmip6-deltas."
                    ) from exc
                print(f"[cmip6] SKIP {our_var:16s} {period:4s} {exp}: {msg[:90]}", flush=True)
        if len(got) == 2:                                  # need both hist + fut for a delta
            out[our_var] = got
    return out


def _open(path: Path, short: str) -> xr.DataArray:
    """Open a CMIP6 zip, return the variable's DataArray averaged over the France box."""
    dest = path.parent / (path.stem + "_x")
    dest.mkdir(exist_ok=True)
    with zipfile.ZipFile(path) as z:
        z.extractall(dest)
    ncs = sorted(dest.glob("*.nc"))
    ds = xr.open_mfdataset([str(n) for n in ncs], combine="by_coords") if len(ncs) > 1 else xr.open_dataset(str(ncs[0]))
    da = ds[short]
    latn = "lat" if "lat" in da.dims else "latitude"
    lonn = "lon" if "lon" in da.dims else "longitude"
    return da.mean(dim=[latn, lonn])               # France-regional signal


def compute_deltas(config: Config, ssp: str, target_year: int, model: str = DEFAULT_MODEL) -> Path:
    """Download + compute per-month quantile deltas, save npz. Returns the npz path."""
    files = download(config, ssp, target_year, model)
    deltas: dict[str, np.ndarray] = {}
    for our_var, (_cds, short, mode) in CMIP6_VARS.items():
        if our_var not in files:
            continue
        hist = _open(files[our_var]["hist"], short).load()
        fut = _open(files[our_var]["fut"], short).load()
        conv = (lambda a: a - 273.15) if our_var == "temperature_c" else (lambda a: a)
        h = pd.Series(conv(hist.values), index=pd.DatetimeIndex(hist["time"].values))
        f = pd.Series(conv(fut.values), index=pd.DatetimeIndex(fut["time"].values))
        d = np.zeros((12, QUANTILES.size))
        for m in range(1, 13):
            hm, fm = h[h.index.month == m].values, f[f.index.month == m].values
            qh, qf = np.quantile(hm, QUANTILES), np.quantile(fm, QUANTILES)
            if mode == "add":
                d[m - 1] = qf - qh
            else:                                    # multiplicative fractional change
                d[m - 1] = np.where(qh > 1e-9, qf / qh - 1.0, 0.0)
        deltas[our_var] = d
    out = config.models_dir / f"cmip6_deltas_{ssp}_{target_year}_{model}.npz"
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez(out, quantiles=QUANTILES, **deltas)
    return out
