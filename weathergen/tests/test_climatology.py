"""Phase 2 unit tests: exact round-trip + residual whiteness (no diurnal/seasonal leftover)."""
from __future__ import annotations

import numpy as np
import pandas as pd
from weathergen._synthetic import synthetic_cube, synthetic_station_meta
from weathergen.climatology import HarmonicSpec

from weathergen import climatology

VARS = ["temperature_c", "wind_speed_ms", "pressure_sea_hpa", "dew_point_c",
        "humidity_pct", "cloud_cover_pct", "precip_1h_mm"]


def test_climatology_roundtrip_and_residual_structure():
    meta = synthetic_station_meta(2)
    cube = synthetic_cube(meta, VARS, start="2015-01-01", periods=24 * 365)
    spec = HarmonicSpec(seasonal=3, diurnal=3, interact=True, use_lst=True)
    clim = climatology.fit(cube, spec)

    anom = clim.standardize(cube)
    recon = clim.reconstruct(anom)
    # exact inverse: reconstruct(standardize(x)) == x
    assert np.nanmax(np.abs(recon.values - cube.values)) < 1e-6

    # temperature residual: ~zero-mean, ~unit-variance, no leftover diurnal/seasonal mean
    vi = VARS.index("temperature_c")
    z = anom.values[:, 0, vi]
    assert abs(np.mean(z)) < 0.05
    assert abs(np.std(z) - 1.0) < 0.1
    t = pd.DatetimeIndex(cube["time"].values)
    by_hour = pd.Series(z).groupby(t.hour).mean()
    by_month = pd.Series(z).groupby(t.month).mean()
    assert by_hour.abs().max() < 0.15      # diurnal cycle removed
    assert by_month.abs().max() < 0.15     # seasonal cycle removed
    # variance also de-trended: binned std ~ 1
    by_hour_std = pd.Series(z).groupby(t.hour).std()
    assert (by_hour_std.between(0.7, 1.4)).all()
