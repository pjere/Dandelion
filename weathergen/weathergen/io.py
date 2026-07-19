"""Phase 1 — data ingestion, quality control, ERA5 fusion, cube construction.

Produces a clean, *flag-tracked* (time, station, variable) cube plus a station
metadata table, ready for the climatology phase. Nothing flagged ever leaks into the
fitted statistics: flagged values become NaN and NaN is excluded from fitting.

Flag codes (companion ``flags`` cube, same shape):
    0  observed (valid)         1  short-gap interpolated (<=3 h)
    2  ERA5 gap-infill          3  ERA5 record extension (pre-station)
   -1  flagged out (QC removed) -9  missing (no data)

ERA5 fusion is *station = truth*: ERA5 is bias-corrected to each station (per month,
mean+std) over the overlap, then used only to (a) extend the record before the station
start and (b) optionally infill long station gaps. Every fill is flagged, never hidden.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import xarray as xr

from .config import Config

# flag codes -----------------------------------------------------------------
F_OBS, F_INTERP, F_ERA5_INFILL, F_ERA5_EXTEND, F_REMOVED, F_MISSING = 0, 1, 2, 3, -1, -9


@dataclass
class QCReport:
    per_station: pd.DataFrame
    notes: list[str] = field(default_factory=list)


@dataclass
class IngestResult:
    cube: xr.DataArray          # fused, QC'd cube used for fitting
    flags: xr.DataArray         # provenance per cell (codes above)
    station_meta: pd.DataFrame
    report: QCReport
    station_cube: xr.DataArray  # QC'd station-only cube (for validation vs observed)


# =========================================================================== #
#  Station cube construction
# =========================================================================== #
def load_station_cube(config: Config, rng: np.random.Generator) -> tuple[xr.DataArray, pd.DataFrame]:
    src = config.section("data").get("source", "synthetic")
    if src == "synthetic":
        from ._synthetic import synthetic_cube, synthetic_station_meta
        meta = synthetic_station_meta(n_stations=3, rng=rng)
        return synthetic_cube(meta, config.var_names, rng=rng), meta
    if src == "pricemodeling_sqlite":
        return _load_from_sqlite(config)
    raise ValueError(f"Unknown data.source: {src!r}")


# backwards-compatible alias used by the scaffold smoke / validation path
def load_cube(config: Config, rng: np.random.Generator) -> tuple[xr.DataArray, pd.DataFrame]:
    return load_station_cube(config, rng)


def _load_from_sqlite(config: Config) -> tuple[xr.DataArray, pd.DataFrame]:
    d = config.section("data")
    db = config.resolve(d["sqlite_path"])
    con = sqlite3.connect(db)
    try:
        stations = pd.read_sql(
            f"SELECT station_id, name, latitude, longitude, altitude AS elevation, region "
            f"FROM {d['station_table']}", con,
        )
        bbox = d["station_filter"]["bbox"]
        m = stations[
            stations.latitude.between(bbox["lat_min"], bbox["lat_max"])
            & stations.longitude.between(bbox["lon_min"], bbox["lon_max"])
        ].copy()
        ids = d["station_filter"].get("ids") or []
        if ids:
            m = m[m.station_id.isin(ids)]
        m = m.sort_values("station_id").reset_index(drop=True)
        m["lst_offset_h"] = m["longitude"] / 15.0

        var_names = config.var_names
        cols = [f"meteo_{sid}_{v}" for sid in m.station_id for v in var_names]
        present = _existing_columns(con, d["master_table"], cols)
        sel = [c for c in cols if c in present]
        df = pd.read_sql(
            f"SELECT ts_utc, {', '.join(sel)} FROM {d['master_table']} "
            f"WHERE ts_utc >= ? AND ts_utc <= ? ORDER BY ts_utc",
            con, params=[d["period"]["start"], d["period"]["end"]],
        )
    finally:
        con.close()

    time = pd.to_datetime(df["ts_utc"], utc=True).dt.tz_convert(None).to_numpy()
    cube = _empty_cube(time, m, var_names)
    for si, sid in enumerate(m.station_id):
        for vi, v in enumerate(var_names):
            col = f"meteo_{sid}_{v}"
            if col in df.columns:
                cube.values[:, si, vi] = pd.to_numeric(df[col], errors="coerce").to_numpy()
    return cube, m


def _existing_columns(con: sqlite3.Connection, table: str, wanted: list[str]) -> set[str]:
    have = {r[1] for r in con.execute(f'PRAGMA table_info("{table}")').fetchall()}
    return {c for c in wanted if c in have}


def _empty_cube(time: np.ndarray, meta: pd.DataFrame, var_names: list[str]) -> xr.DataArray:
    data = np.full((len(time), len(meta), len(var_names)), np.nan)
    return xr.DataArray(
        data, dims=("time", "station", "variable"),
        coords={
            "time": time, "station": meta.station_id.to_numpy(), "variable": list(var_names),
            "latitude": ("station", meta.latitude.to_numpy()),
            "longitude": ("station", meta.longitude.to_numpy()),
            "elevation": ("station", meta.elevation.to_numpy()),
            "lst_offset_h": ("station", meta.lst_offset_h.to_numpy()),
        },
        name="obs", attrs={"tz": "UTC"},
    )


# =========================================================================== #
#  Quality control
# =========================================================================== #
def qc(cube: xr.DataArray, config: Config) -> tuple[xr.DataArray, xr.DataArray, QCReport]:
    """Range + flat-line + spike + duplicate checks, then short-gap interpolation.

    Returns (clean_cube, flags, report). Flagged cells are set to NaN (never imputed
    into the training set); short gaps (<=3 h) are interpolated and flagged.
    """
    var_cfg = config.variables
    notes: list[str] = []

    # de-duplicate timestamps (defensive)
    if cube.indexes["time"].has_duplicates:
        dup = int(cube.indexes["time"].duplicated().sum())
        cube = cube.sel(time=~cube.indexes["time"].duplicated())
        notes.append(f"Removed {dup} duplicate timestamps.")

    out = cube.copy()
    flags = xr.full_like(cube, F_OBS, dtype="int16")
    flags = flags.where(~cube.isnull(), F_MISSING)

    counts = {v: {"range": 0, "flatline": 0, "spike": 0} for v in map(str, cube["variable"].values)}
    for vi, v in enumerate(map(str, cube["variable"].values)):
        cfg = var_cfg[v]
        lo, hi = cfg["bounds"]
        zero_like = bool(cfg.get("zero_inflated") or cfg.get("kind") == "intermittent")
        for si in range(cube.sizes["station"]):
            x = out.values[:, si, vi]
            r = ((x < lo) | (x > hi)) & ~np.isnan(x)          # range
            fl = _flatline_mask(x, min_run=24, ignore_zero=zero_like)  # stuck sensor
            sp = _spike_mask(x, thr=8.0)                       # isolated spikes
            removed = r | fl | sp
            x[removed] = np.nan
            flags.values[removed, si, vi] = F_REMOVED
            counts[v]["range"] += int(r.sum())
            counts[v]["flatline"] += int(fl.sum())
            counts[v]["spike"] += int(sp.sum())

    # short-gap interpolation (<= 3 h) ; longer gaps stay NaN
    before = out.isnull()
    out = out.interpolate_na(dim="time", method="linear", max_gap=np.timedelta64(3, "h"))
    interp_filled = before & ~out.isnull()
    flags = flags.where(~interp_filled, F_INTERP)
    notes.append(f"QC flag counts per variable: {counts}")
    notes.append("Short gaps (<=3 h) linearly interpolated; longer gaps kept as NaN (excluded from fit).")

    pct_missing = (out.isnull().mean(dim="time") * 100).to_pandas().mean(axis=1).rename("pct_missing")
    per_station = pct_missing.to_frame()
    return out, flags, QCReport(per_station=per_station, notes=notes)


def _flatline_mask(x: np.ndarray, min_run: int, ignore_zero: bool) -> np.ndarray:
    """Flag runs of >= ``min_run`` identical non-NaN values (stuck sensor).

    For intermittent/zero-inflated variables, legitimate zero runs are not flagged.
    """
    mask = np.zeros(x.size, dtype=bool)
    n = x.size
    i = 0
    while i < n:
        if np.isnan(x[i]):
            i += 1
            continue
        j = i + 1
        while j < n and (x[j] == x[i]):
            j += 1
        run = j - i
        if run >= min_run and not (ignore_zero and x[i] == 0.0):
            mask[i:j] = True
        i = j
    return mask


def _spike_mask(x: np.ndarray, thr: float) -> np.ndarray:
    """Flag isolated spikes via a robust z on first differences (up-then-down)."""
    if np.isnan(x).all():
        return np.zeros(x.size, dtype=bool)
    d = np.diff(x)
    if np.isnan(d).all():
        return np.zeros(x.size, dtype=bool)
    med = np.nanmedian(d)
    mad = np.nanmedian(np.abs(d - med)) or 1e-9
    z = 0.6745 * (d - med) / mad
    mask = np.zeros(x.size, dtype=bool)
    big = np.abs(z) > thr
    # a spike at t: large jump into t and large opposite jump out of t
    for t in range(1, x.size - 1):
        if big[t - 1] and big[t] and np.sign(d[t - 1]) != np.sign(d[t]):
            mask[t] = True
    return mask


# =========================================================================== #
#  ERA5 ingestion + fusion (station = truth, ERA5 = extend)
# =========================================================================== #
def load_era5(config: Config, meta: pd.DataFrame) -> xr.DataArray | None:
    """Load ERA5 collocated to station coords as a (time, station, variable) cube.

    Reads a provided NetCDF (``source: file``) — gridded (interpolated to stations) or
    already collocated — or downloads via the CDS API (``source: cds``). Returns None if
    ERA5 is disabled or unavailable (a loud note is added to the report by the caller).
    """
    e = config.section("data").get("era5", {})
    if not e.get("enabled"):
        return None
    if e.get("source") == "arco":
        from .era5_arco import load_arco_cube  # fast point time-series path
        return load_arco_cube(config, meta)
    if e.get("source") == "cds":
        return _download_era5_cds(config, meta, e)     # gridded monthly (slow); lazy
    path = config.resolve(e.get("path"))
    if path is None or not Path(path).exists():
        return None
    ds = xr.open_dataset(path)
    return _era5_to_station_cube(ds, meta, config, e)


def _era5_to_station_cube(ds: xr.Dataset, meta: pd.DataFrame, config: Config, e: dict) -> xr.DataArray:
    vmap: dict[str, str] = e["variable_map"]
    alias = _resolve_era5_names(ds, vmap)             # config short-name -> actual nc var
    # collocate to stations
    if not e.get("collocated"):
        latn = "latitude" if "latitude" in ds.coords else "lat"
        lonn = "longitude" if "longitude" in ds.coords else "lon"
        ds = ds.interp({latn: ("station", meta.latitude.values),
                        lonn: ("station", meta.longitude.values)})
        ds = ds.assign_coords(station=("station", meta.station_id.values))
    time = pd.to_datetime(ds["time"].values)
    var_names = config.var_names
    cube = _empty_cube(time.to_numpy(), meta, var_names)

    raw: dict[str, np.ndarray] = {}
    for short, target in vmap.items():
        if short in alias and alias[short] in ds:
            raw[target] = np.asarray(ds[alias[short]].transpose("time", "station").values, dtype="float64")

    for vi, v in enumerate(var_names):
        cube.values[:, :, vi] = _era5_variable(v, raw)
    cube.attrs["source"] = "era5"
    return cube


def _era5_variable(v: str, raw: dict[str, np.ndarray]) -> np.ndarray:
    """Map+convert ERA5 fields to one internal variable (unit conversions documented)."""
    if v in raw:
        a = raw[v]
        if v in ("temperature_c", "dew_point_c"):
            return a - 273.15                       # K -> °C
        if v == "pressure_sea_hpa":
            return a / 100.0                        # Pa -> hPa
        if v in ("cloud_cover_pct", "cloud_cover_low"):
            return a * 100.0                        # fraction -> %
        if v == "precip_1h_mm":
            return np.clip(a * 1000.0, 0, None)     # m -> mm
        return a
    if v == "wind_speed_ms" and "_u_wind" in raw and "_v_wind" in raw:
        return np.hypot(raw["_u_wind"], raw["_v_wind"])
    if v == "humidity_pct" and "temperature_c" in raw and "dew_point_c" in raw:
        t, td = raw["temperature_c"] - 273.15, raw["dew_point_c"] - 273.15
        es = 6.112 * np.exp(17.62 * t / (243.12 + t))
        e = 6.112 * np.exp(17.62 * td / (243.12 + td))
        return np.clip(100.0 * e / es, 0, 100)      # Magnus RH
    # ERA5 has no source for this variable
    shape = next(iter(raw.values())).shape if raw else (0, 0)
    return np.full(shape, np.nan)


def _resolve_era5_names(ds: xr.Dataset, vmap: dict[str, str]) -> dict[str, str]:
    """Tolerant resolver: CDS short names vs NetCDF variable names (2t/t2m, 10u/u10, ...)."""
    aliases = {
        "2t": ["2t", "t2m"], "2d": ["2d", "d2m"], "msl": ["msl"],
        "10u": ["10u", "u10"], "10v": ["10v", "v10"],
        "tcc": ["tcc"], "lcc": ["lcc"], "tp": ["tp"],
    }
    out = {}
    for short in vmap:
        for cand in aliases.get(short, [short]):
            if cand in ds.variables:
                out[short] = cand
                break
    return out


def _download_era5_cds(config: Config, meta: pd.DataFrame, e: dict) -> xr.DataArray:
    """Download the full ERA5 record (era5.start -> period end) via CDS, then collocate."""
    from . import era5_cds
    start = pd.Timestamp(e.get("start", "1979-01-01")).year
    end = pd.Timestamp(config.section("data")["period"]["end"]).year
    files = era5_cds.download_months(config, era5_cds.month_list(start, end))
    ds = era5_cds.open_era5(files)
    return _era5_to_station_cube(ds, meta, config, e)


def fuse_station_era5(
    station: xr.DataArray, station_flags: xr.DataArray, era5: xr.DataArray, config: Config
) -> tuple[xr.DataArray, xr.DataArray, dict[str, Any]]:
    """Bias-correct ERA5 to each station (per month, mean+std) over the overlap, then
    extend the record before the station start and (optionally) infill long station gaps.
    """
    fcfg = config.section("data")["era5"]["fusion"]
    info: dict[str, Any] = {"bias_correction": fcfg.get("bias_correction"), "mode": fcfg.get("mode")}

    era5_bc = _bias_correct_monthly(station, era5)        # ERA5 on station scale

    # union time axis (ERA5 typically starts earlier)
    full_time = np.union1d(era5_bc["time"].values, station["time"].values)
    fused = station.reindex(time=full_time)
    flags = station_flags.reindex(time=full_time, fill_value=F_MISSING)
    era5_al = era5_bc.reindex(time=full_time)

    need = fused.isnull()
    if fcfg.get("mode") == "extend":
        station_start = station["time"].values.min()
        before = xr.DataArray(full_time < station_start, dims="time", coords={"time": full_time})
        take = need & before & era5_al.notnull()
        fused = fused.where(~take, era5_al)
        flags = flags.where(~take, F_ERA5_EXTEND)
        info["extension_from"] = str(pd.Timestamp(full_time.min()).date())
    if fcfg.get("infill_long_gaps"):
        take = fused.isnull() & era5_al.notnull()
        n_infill = int(take.sum())
        fused = fused.where(~take, era5_al)
        flags = flags.where(~take, F_ERA5_INFILL)
        info["gap_infill_cells"] = n_infill

    info["n_years_fused"] = round(fused.sizes["time"] / 8760, 1)
    return fused, flags, info


def _bias_correct_monthly(station: xr.DataArray, era5: xr.DataArray) -> xr.DataArray:
    """Match ERA5 to station per (station, variable, month): z-score on ERA5, rescale
    to station mean+std. DECISION P1: light mean+std matching; upgradeable to quantile
    mapping. Returns ERA5 in station units/scale on ERA5's own time axis.
    """
    common = [v for v in map(str, station["variable"].values)
              if v in set(map(str, era5["variable"].values))]
    era5 = era5.sel(variable=common)
    months = list(range(1, 13))
    s_mean = station.groupby(station["time"].dt.month).mean("time").reindex(month=months)
    s_std = station.groupby(station["time"].dt.month).std("time").reindex(month=months)
    e_mean = era5.groupby(era5["time"].dt.month).mean("time").reindex(month=months)
    e_std = era5.groupby(era5["time"].dt.month).std("time").reindex(month=months)
    e_std = e_std.where(e_std > 1e-9, 1.0)
    # months the station never observed -> identity correction (fall back to ERA5's own stats)
    s_mean = s_mean.where(s_mean.notnull(), e_mean)
    s_std = s_std.where(s_std.notnull(), e_std)

    # map each ERA5 timestamp's month to its monthly stats (vectorized, no per-month loop)
    em = era5["time"].dt.month
    z = (era5 - e_mean.sel(month=em)) / e_std.sel(month=em)
    return z * s_std.sel(month=em) + s_mean.sel(month=em)


# =========================================================================== #
#  Orchestrator
# =========================================================================== #
def build_dataset(config: Config, rng: np.random.Generator) -> IngestResult:
    """Full Phase 1: load station data -> QC -> (optional) ERA5 fusion -> flagged cube."""
    cube, meta = load_station_cube(config, rng)
    station_clean, station_flags, report = qc(cube, config)

    fused, flags = station_clean, station_flags
    era5 = load_era5(config, meta)
    if era5 is None:
        report.notes.append(
            "ERA5 NOT available (no file / disabled) — record NOT extended. "
            "Tails and climatology rest on the short station record only (loud warning carried to validation)."
        )
        report.per_station["era5_extended"] = False
    else:
        fused, flags, info = fuse_station_era5(station_clean, station_flags, era5, config)
        report.notes.append(f"ERA5 fusion: {info}")
        report.per_station["era5_extended"] = True

    return IngestResult(
        cube=fused, flags=flags, station_meta=meta, report=report, station_cube=station_clean,
    )
