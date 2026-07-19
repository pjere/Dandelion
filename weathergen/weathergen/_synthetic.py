"""Tiny synthetic multi-site weather cube for the end-to-end smoke test.

Not a model — just plausible-looking data (diurnal + seasonal cycle + AR(1) noise)
so the pipeline wiring can be exercised in <1 min without touching the real database.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import xarray as xr


def synthetic_era5(
    station_meta: pd.DataFrame,
    start: str = "2005-01-01",
    periods: int = 24 * 365 * 12,            # ~12 years hourly, overlaps + extends the station record
    rng: np.random.Generator | None = None,
) -> xr.Dataset:
    """A tiny gridded ERA5-like dataset (CDS NetCDF var names) for fusion tests.

    Vars: t2m,d2m (K), msl (Pa), u10,v10 (m/s), tcc,lcc (0-1), tp (m). Carries a
    deliberate bias vs the station synthetic so bias-correction has work to do.
    """
    rng = rng or np.random.default_rng(1)
    time = pd.date_range(start, periods=periods, freq="1h")
    lats = np.linspace(station_meta.latitude.min() - 1, station_meta.latitude.max() + 1, 5)
    lons = np.linspace(station_meta.longitude.min() - 1, station_meta.longitude.max() + 1, 5)
    doy = time.dayofyear.to_numpy()
    season = np.cos(2 * np.pi * (doy - 200) / 365.25)[:, None, None]
    T, La, Lo = len(time), len(lats), len(lons)
    g = rng.standard_normal((T, La, Lo)) * 0.5

    def fld(mean, amp, scale=1.0):
        return (mean + amp * season + scale * g).astype("float32")

    ds = xr.Dataset(
        {
            "t2m": (("time", "latitude", "longitude"), fld(286.0, 9.0)),    # K, +1°C warm bias
            "d2m": (("time", "latitude", "longitude"), fld(282.0, 7.0)),
            "msl": (("time", "latitude", "longitude"), fld(101400.0, 600.0, 100.0)),  # Pa
            "u10": (("time", "latitude", "longitude"), fld(1.0, 0.5)),
            "v10": (("time", "latitude", "longitude"), fld(1.0, 0.5)),
            "tcc": (("time", "latitude", "longitude"), np.clip(fld(0.55, 0.1, 0.1), 0, 1)),
            "lcc": (("time", "latitude", "longitude"), np.clip(fld(0.35, 0.1, 0.1), 0, 1)),
            "tp": (("time", "latitude", "longitude"), np.clip(fld(0.0003, 0.0001, 0.0005), 0, None)),
        },
        coords={"time": time, "latitude": lats, "longitude": lons},
    )
    return ds


def synthetic_station_meta(n_stations: int = 3, rng: np.random.Generator | None = None) -> pd.DataFrame:
    rng = rng or np.random.default_rng(0)
    lats = rng.uniform(43.0, 50.0, n_stations)
    lons = rng.uniform(-2.0, 6.0, n_stations)
    return pd.DataFrame({
        "station_id": [f"SYN{ i:03d}" for i in range(n_stations)],
        "name": [f"SYNTH-{i}" for i in range(n_stations)],
        "latitude": lats,
        "longitude": lons,
        "elevation": rng.uniform(10, 400, n_stations),
        "lst_offset_h": lons / 15.0,            # local-solar-time offset
    })


def synthetic_cube(
    station_meta: pd.DataFrame,
    var_names: list[str],
    start: str = "2015-01-01",
    periods: int = 24 * 60,                      # 60 days hourly
    rng: np.random.Generator | None = None,
) -> xr.DataArray:
    """Return a (time, station, variable) DataArray of plausible synthetic weather."""
    rng = rng or np.random.default_rng(0)
    time = pd.date_range(start, periods=periods, freq="1h", tz="UTC")
    doy = time.dayofyear.to_numpy()
    hod = time.hour.to_numpy()
    S, V, T = len(station_meta), len(var_names), len(time)
    data = np.empty((T, S, V), dtype="float64")

    # crude per-variable signal templates
    base = {
        "temperature_c": (12.0, 9.0, 5.0, 1.5),   # mean, seasonal amp, diurnal amp, noise
        "dew_point_c": (8.0, 7.0, 3.0, 1.2),
        "pressure_sea_hpa": (1015.0, 6.0, 1.0, 3.0),
        "wind_speed_ms": (4.0, 1.5, 1.0, 1.0),
        "humidity_pct": (75.0, 8.0, 12.0, 5.0),
        "cloud_cover_pct": (55.0, 10.0, 10.0, 15.0),
        "precip_1h_mm": (0.2, 0.1, 0.05, 0.4),
    }
    for s in range(S):
        off = float(station_meta.iloc[s]["lst_offset_h"])
        for v, name in enumerate(var_names):
            mean, samp, damp, noise = base.get(name, (0.0, 1.0, 0.5, 1.0))
            seasonal = samp * np.cos(2 * np.pi * (doy - 200) / 365.25)
            diurnal = damp * np.cos(2 * np.pi * ((hod + off) - 15) / 24.0)
            # AR(1) coloured noise
            e = rng.standard_normal(T)
            ar = np.empty(T)
            ar[0] = e[0]
            for t in range(1, T):
                ar[t] = 0.85 * ar[t - 1] + e[t]
            series = mean + seasonal + diurnal + noise * ar / np.sqrt(1 / (1 - 0.85**2))
            if name in ("humidity_pct", "cloud_cover_pct"):
                series = np.clip(series, 0, 100)
            if name in ("wind_speed_ms",):
                series = np.clip(series, 0, None)
            if name == "precip_1h_mm":
                # intermittent: mostly dry
                wet = rng.random(T) < 0.15
                series = np.where(wet, np.clip(series, 0, None) * 5.0, 0.0)
            data[:, s, v] = series

    cube = xr.DataArray(
        data,
        dims=("time", "station", "variable"),
        coords={
            "time": time.tz_convert(None).to_numpy(),   # store naive UTC
            "station": station_meta["station_id"].to_numpy(),
            "variable": list(var_names),
            "latitude": ("station", station_meta["latitude"].to_numpy()),
            "longitude": ("station", station_meta["longitude"].to_numpy()),
            "elevation": ("station", station_meta["elevation"].to_numpy()),
            "lst_offset_h": ("station", station_meta["lst_offset_h"].to_numpy()),
        },
        name="obs",
        attrs={"tz": "UTC", "source": "synthetic"},
    )
    return cube
