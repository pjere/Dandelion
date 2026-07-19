"""Canonical weathergen-cube reader (§5) — the single home for cube loading, replacing the 3 duplicate
loaders in demand/res/availability.

The cube is `simulation.nc` (`obs` DataArray over dims time × station × variable, optional ensemble dim).
Reductions provided: national daily temperature + annual wetness (availability/derating side) and a tidy
per-station frame (demand/res side). Logic is byte-for-byte the previous per-model loaders so the golden
baseline is preserved on rewiring.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

_ENSEMBLE_DIMS = ("realization", "member", "draw", "ensemble")


def open_cube(path):
    from pathlib import Path

    import xarray as xr
    if not Path(path).exists():
        raise FileNotFoundError(f"weathergen cube not found: {path}")
    return xr.open_dataset(path)


def cube_variables(path) -> list[str]:
    """The cube's variable names (cheap — reads only the coordinate)."""
    ds = open_cube(path)
    try:
        return [str(v) for v in ds["obs"]["variable"].values]
    finally:
        ds.close()


def _select_realization(ds, realization: int):
    for dim in _ENSEMBLE_DIMS:
        if dim in ds.dims:
            return ds.isel({dim: realization % ds.sizes[dim]})
    return ds


def load_national_weather(path, realization: int = 0) -> tuple[pd.Series, dict[int, float]]:
    """→ (national daily-mean temperature °C, wetness_by_year = annual precip / mean annual precip)."""
    ds = _select_realization(open_cube(path), realization)
    try:
        da = ds["obs"]
        time = pd.to_datetime(da["time"].values)
        if time.tz is None:
            time = time.tz_localize("UTC")
        vmap = {str(v): i for i, v in enumerate(da["variable"].values)}
        arr = da.transpose("time", "station", "variable").values
        temp = np.nanmean(arr[:, :, vmap["temperature_c"]], axis=1)
        precip = np.nanmean(arr[:, :, vmap["precip_1h_mm"]], axis=1)
    finally:
        ds.close()
    temp_daily = pd.Series(temp, index=time).resample("1D").mean()
    annual = pd.Series(precip, index=time).resample("1YE").sum()
    mean_annual = annual.mean()
    wetness = {int(ts.year): float(v / mean_annual) if mean_annual else 1.0 for ts, v in annual.items()}
    return temp_daily, wetness


def load_station_tidy(path, variables, realization: int = 0) -> tuple[pd.DataFrame, pd.DataFrame]:
    """→ (tidy [timestamp_utc, station_id, <variables>], stations metadata) — demand/res side."""
    ds = _select_realization(open_cube(path), realization)
    try:
        da = ds["obs"]
        time = pd.to_datetime(da["time"].values)
        if time.tz is None:
            time = time.tz_localize("UTC")
        var_names = [str(v) for v in da["variable"].values]
        stn = [str(s) for s in da["station"].values]
        arr = da.transpose("time", "station", "variable").values
        stations = pd.DataFrame({"station_id": stn,
                                 "latitude": np.asarray(ds["latitude"].values, float),
                                 "longitude": np.asarray(ds["longitude"].values, float),
                                 "altitude": np.asarray(ds["elevation"].values, float)})
    finally:
        ds.close()
    vmap = {v: i for i, v in enumerate(var_names)}
    frames = []
    for j, sid in enumerate(stn):
        block = pd.DataFrame({"timestamp_utc": time, "station_id": sid})
        for v in variables:
            block[v] = arr[:, j, vmap[v]] if v in vmap else np.nan
        frames.append(block)
    return pd.concat(frames, ignore_index=True), stations
