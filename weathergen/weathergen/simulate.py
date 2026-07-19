"""Phase 7 — simulation engine. Inverse of the fitting chain.

latent VAR/AR -> EOF reconstruct -> copula uniforms -> F^-1 marginals -> transforms^-1
-> reseasonalize (climatology) -> trend (QDM) -> physical constraints. Seeded and
deterministic: same seed + config => identical output.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import xarray as xr
from scipy.special import ndtr  # standard-normal CDF

from ._cube import from_matrix
from .config import Config
from .model import FittedModel


def simulate(model: FittedModel, config: Config, rng: np.random.Generator) -> xr.DataArray:
    """Generate ``simulate.horizon_years`` of hourly weather for all stations/variables."""
    scfg = config.section("simulate")
    horizon = int(scfg["horizon_years"])
    start_year = int(scfg["start_year"])
    time = pd.date_range(f"{start_year}-01-01", periods=horizon * 8760, freq="1h")

    # 1-2) latent reduced series -> full Gaussian latent field (n, D); pass the calendar month of
    #      each step so the (seasonal) innovation covariance tracks the season (D5.3)
    gauss = model.dependence.simulate(time.month.to_numpy(), rng)
    # 3) copula uniforms -> inverse marginals -> standardized anomaly matrix
    u = ndtr(gauss)
    anom_mat = model.marginals.from_uniform(u)
    keys = [(s, v) for s in model.station_ids for v in model.var_names]
    anom = from_matrix(anom_mat, keys, np.asarray(time))
    # 4) reseasonalize (climatology) then invert the per-variable transform (see D3.1)
    g = model.climatology.reconstruct(anom)
    cube = model.transforms.inverse(g)
    # carry station metadata coords
    for c in ("latitude", "longitude", "elevation", "lst_offset_h"):
        if c in model.station_meta.columns:
            cube = cube.assign_coords({c: ("station", model.station_meta.set_index("station_id").loc[
                list(map(str, cube["station"].values)), c].to_numpy())})
    # 5) external trend
    cube = model.trend.apply(cube)
    # 6) physical constraints
    cube, clip_log = _enforce_constraints(cube, config)
    # 6b) derived diagnostics (D3.2): RH from temperature + dew point (Magnus)
    cube = _derive_humidity(cube)
    # 6c) co-generate 100 m wind conditioned on the simulated 10 m wind (for step iv renewables)
    cube = _append_wind100(cube, config, rng)

    cube.attrs.update({
        "generator": "weathergen", "seed": config.seed, "horizon_years": horizon,
        "start_year": start_year, "trend_enabled": int(model.trend.enabled),
        "clips": str(clip_log),
        "provenance": "fit-once/simulate-many; config + seed embedded for reproducibility",
    })
    return cube


def _append_wind100(cube: xr.DataArray, config: Config, rng: np.random.Generator) -> xr.DataArray:
    """Co-generate 100 m wind (transfer on simulated 10 m + spatial-AR residual) if a model is set."""
    rel = config.section("simulate").get("wind100_model")
    if not rel:
        return cube
    path = config.path.parent / rel if not str(rel).startswith("/") else rel
    from pathlib import Path
    if not Path(path).exists():
        return cube
    from .wind100 import Wind100Model
    return Wind100Model.load(path).append(cube, rng)


def _derive_humidity(cube: xr.DataArray) -> xr.DataArray:
    """Append relative humidity (%) derived from temperature + dew point (Magnus, D3.2)."""
    vs = set(map(str, cube["variable"].values))
    if not {"temperature_c", "dew_point_c"} <= vs or "humidity_pct" in vs:
        return cube
    t = cube.sel(variable="temperature_c")
    td = cube.sel(variable="dew_point_c")
    es = 6.112 * np.exp(17.62 * t / (243.12 + t))
    e = 6.112 * np.exp(17.62 * td / (243.12 + td))
    rh = (100.0 * e / es).clip(0, 100)
    rh = rh.expand_dims(variable=["humidity_pct"]).transpose("time", "station", "variable")
    return xr.concat([cube, rh], dim="variable")


def _enforce_constraints(cube: xr.DataArray, config: Config) -> tuple[xr.DataArray, dict]:
    """Clip to physical bounds; every clip is counted and surfaced (never hidden)."""
    if not config.section("simulate").get("enforce_constraints", True):
        return cube, {}
    out = cube.copy()
    clip_log: dict[str, int] = {}
    for v in cube["variable"].values:
        vc = config.variables[str(v)]
        lo, hi = vc["bounds"]
        sl = out.sel(variable=v)
        n_clip = int(((sl < lo) | (sl > hi)).sum())
        if n_clip:
            clip_log[str(v)] = n_clip
        sl = sl.clip(lo, hi)
        # intermittent: dry hours already map to ~0 via the marginal DRY_SENTINEL; only a light
        # floor removes tiny reconstruction noise (keeps genuine light precip, D4/D8.1 hurdle)
        if vc.get("kind") == "intermittent":
            sl = sl.where(sl >= 0.02, 0.0)
        out.loc[{"variable": v}] = sl
    return out, clip_log
