"""Phase 6 unit test: QDM imposes the prescribed external trend (mean drift + tail
intensification + smooth transition), and is a no-op when disabled."""
from __future__ import annotations

import numpy as np
import pandas as pd
import xarray as xr
from weathergen.trend import Trend


def _temp_cube(rng):
    time = pd.date_range("2027-01-01", periods=20 * 8760, freq="1h")
    x = rng.normal(12.0, 5.0, len(time))
    return xr.DataArray(
        x[:, None, None], dims=("time", "station", "variable"),
        coords={"time": time.to_numpy(), "station": ["S0"], "variable": ["temperature_c"]},
        name="obs",
    )


def test_trend_off_is_noop():
    rng = np.random.default_rng(0)
    cube = _temp_cube(rng)
    assert np.allclose(Trend(enabled=False).apply(cube).values, cube.values)


def test_trend_qdm_mean_drift_tail_and_smoothness():
    rng = np.random.default_rng(0)
    cube = _temp_cube(rng)
    q = np.linspace(0.05, 0.95, 19)
    # delta grows with quantile: +1°C at low tail, +2 median, +4 at high tail (intensification)
    dq = np.interp(q, [0.05, 0.5, 0.95], [1.0, 2.0, 4.0])
    tr = Trend(enabled=True, baseline_year=2027, target_year=2050, quantiles=q,
               deltas={"temperature_c": np.tile(dq, (12, 1))},
               mode={"temperature_c": "add"}, trend_variability=True)
    out = tr.apply(cube)

    yr = pd.DatetimeIndex(cube["time"].values).year
    o, x = out.values[:, 0, 0], cube.values[:, 0, 0]
    late = 2044

    # decadal mean drift ~ mean(delta) * frac
    drift = o[yr == late].mean() - o[yr == 2027].mean()
    assert 1.0 < drift < 2.6
    # tail intensification: 95th-pct drift exceeds median drift
    p95d = np.quantile(o[yr == late], 0.95) - np.quantile(x[yr == late], 0.95)
    p50d = np.quantile(o[yr == late], 0.50) - np.quantile(x[yr == late], 0.50)
    assert p95d > p50d + 0.5
    # smooth transition: annual means essentially non-decreasing
    ann = np.array([o[yr == y].mean() for y in range(2027, late + 1)])
    assert np.all(np.diff(ann) > -0.25)
    # first year ~ baseline (frac ~ 0)
    assert abs(o[yr == 2027].mean() - x[yr == 2027].mean()) < 0.3
