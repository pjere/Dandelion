"""Phase 1 unit tests: QC on a crafted dirty fixture + ERA5 fusion mechanics."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr
from weathergen._synthetic import synthetic_cube, synthetic_era5, synthetic_station_meta
from weathergen.config import load_config

from weathergen import io

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"


def _cfg():
    cfg = load_config(CONFIG_PATH)
    cfg.raw["data"]["source"] = "synthetic"
    return cfg


def test_qc_flags_dirty_fixture():
    """Out-of-range, stuck-sensor, spike and duplicate timestamps are removed (NaN)
    and flagged; short gaps are interpolated; nothing dirty leaks into the clean cube."""
    cfg = _cfg()
    meta = synthetic_station_meta(2)
    cube = synthetic_cube(meta, cfg.var_names, periods=24 * 20)  # 20 days
    vi = cfg.var_names.index("temperature_c")
    spike_val = cube.values[299, 0, vi] + 60.0

    # inject faults on station 0, temperature
    cube.values[100:106, 0, vi] = 999.0            # 6 h out-of-range block (> short gap)
    cube.values[200:230, 0, vi] = 7.77             # 30 h stuck flat-line
    cube.values[300, 0, vi] = spike_val            # isolated spike up...
    cube.values[301, 0, vi] = cube.values[299, 0, vi]          # ...back down
    cube.values[400:402, 0, vi] = np.nan           # 2 h gap -> interpolated

    clean, flags, report = io.qc(cube, cfg)
    # 6 h range block: removed and (gap > 3 h) left as NaN -> never leaks into fit
    assert np.isnan(clean.values[103, 0, vi])
    assert flags.values[103, 0, vi] == io.F_REMOVED
    # 30 h flat-line: removed, stays NaN
    assert np.isnan(clean.values[215, 0, vi])
    # spike: the bad magnitude is gone (removed; the 1 h hole is then interpolated)
    assert clean.values[300, 0, vi] < spike_val - 30
    # short 2 h gap: interpolated and flagged
    assert not np.isnan(clean.values[400, 0, vi])
    assert flags.values[400, 0, vi] == io.F_INTERP
    assert "pct_missing" in report.per_station.columns


def test_era5_fusion_extends_and_bias_corrects(tmp_path):
    """ERA5 (warm-biased) is bias-corrected to the station and extends the record
    backward; the corrected ERA5 overlap mean matches the station mean."""
    cfg = _cfg()
    meta = synthetic_station_meta(2)
    # station record: 2015 only
    station = synthetic_cube(meta, cfg.var_names, start="2015-01-01", periods=24 * 365)
    # ERA5: 2010-2016 gridded, +1°C warm bias
    ds = synthetic_era5(meta, start="2010-01-01", periods=24 * 365 * 6)
    nc = tmp_path / "era5.nc"
    ds.to_netcdf(nc)
    cfg.raw["data"]["era5"]["path"] = str(nc)
    cfg.raw["data"]["era5"]["collocated"] = False

    era5_cube = io.load_era5(cfg, meta)
    assert era5_cube is not None
    sflags = xr.full_like(station, io.F_OBS, dtype="int16")
    fused, fflags, info = io.fuse_station_era5(station, sflags, era5_cube, cfg)

    # record extended before 2015
    assert pd.Timestamp(fused["time"].values.min()).year <= 2010
    assert (fflags.values == io.F_ERA5_EXTEND).any()
    # bias correction: corrected ERA5 temperature mean over overlap ~ station mean
    overlap = slice("2015-01-01", "2015-12-31")
    s_mean = float(station.sel(variable="temperature_c").mean())
    f_mean = float(fused.sel(variable="temperature_c", time=overlap).mean())
    assert abs(s_mean - f_mean) < 0.5
