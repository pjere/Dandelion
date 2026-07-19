"""DM Phase 5 — read the weathergen NetCDF cube and build national projection features.

The cube is ``obs(time, station, variable)`` with the variables the feature builder needs
(temperature_c, wind_speed_ms, cloud_cover_pct, humidity_pct) plus station lat/lon/elevation.
An optional ensemble dimension (realization/member/draw) is selected per weather draw.
Output features reuse the *calibration* feature builder so history and projection are identical
by construction; the only projection-specific step is freezing the linear trend at the anchor year.
"""
from __future__ import annotations

import pandas as pd

from powersim_core import lake

from ..config import Config
from ..features.build import build_features
from ..io.loaders import WEATHER_VARS

_ENSEMBLE_DIMS = ("realization", "member", "draw", "ensemble")


def open_cube(config: Config):
    import xarray as xr
    path = config.resolve(config.section("projection")["weathergen_output"])
    if not path.exists():
        raise FileNotFoundError(f"weathergen cube not found: {path} (run the weather generator first)")
    return xr.open_dataset(path)


def n_realizations(ds) -> int:
    for d in _ENSEMBLE_DIMS:
        if d in ds.dims:
            return int(ds.sizes[d])
    return 1


def _tidy_weather(config: Config, realization: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Cube (one realization) -> tidy per-station weather frame + station metadata.

    Delegates to the shared powersim_core cube reader (single loader for demand/res/availability); the
    demand-specific step is clipping percentage variables to [0, 100]."""
    from powersim_core.weather_cube import load_station_tidy
    path = config.resolve(config.section("projection")["weathergen_output"])
    tidy, stations = load_station_tidy(path, WEATHER_VARS, realization)
    for b in ("cloud_cover_pct", "humidity_pct"):
        if b in tidy:
            tidy[b] = tidy[b].clip(0, 100)
    return tidy, stations


def projection_features(config: Config, realization: int = 0, force: bool = False) -> pd.DataFrame:
    """National feature frame for one weather realization, trend frozen at the anchor year."""
    cache = lake.table_path("demand", "projection_features", realization=realization)
    cube_path = config.resolve(config.section("projection")["weathergen_output"])
    fresh = (cache.exists() and cube_path and cube_path.exists()
             and cache.stat().st_mtime >= cube_path.stat().st_mtime)
    if fresh and not force:                          # invalidate if the cube was regenerated
        return lake.read_table("demand", "projection_features", realization=realization)

    weather, stations = _tidy_weather(config, realization)
    feat = build_features(config, weather, stations)

    # freeze the calibrated linear trend at the anchor year (drivers, not the historical
    # trend, carry the base forward — see phase-5 Q&A). trend_years is in the CALIBRATION
    # coordinate (years since the history start), so recompute the anchor value there.
    start = pd.Timestamp(config.section("data")["period"]["start"], tz="UTC")
    anchor = pd.Timestamp(f"{config.section('projection')['anchor_year']}-07-01", tz="UTC")
    feat["trend_years"] = (anchor - start).total_seconds() / (365.25 * 24 * 3600)

    # warm-up fill: the first day(s) of the horizon have undefined lagged daily temps (D-1/D-2
    # via shift). Backfill so the projected series is continuous with no NaN (immaterial over 20 yr).
    for c in ("T_lag_d1", "T_lag_d2"):
        if c in feat:
            feat[c] = feat[c].bfill()

    lake.write_table(feat, "demand", "projection_features", realization=realization)
    return feat
