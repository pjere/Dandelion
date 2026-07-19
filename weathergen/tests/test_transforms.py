"""Phase 3 unit tests: exact invertibility + normalization behaviour."""
from __future__ import annotations

import numpy as np
import pytest
from scipy import stats
from weathergen._synthetic import synthetic_cube, synthetic_station_meta
from weathergen.transforms import Log1p, Logit, YeoJohnson

from weathergen import transforms

VARS = ["temperature_c", "wind_speed_ms", "pressure_sea_hpa", "dew_point_c",
        "humidity_pct", "cloud_cover_pct", "precip_1h_mm"]


@pytest.mark.parametrize("lmbda", [0.0, 0.3, 0.7, 1.0, 1.5, 2.0])
def test_yeojohnson_roundtrip(lmbda):
    rng = np.random.default_rng(0)
    x = rng.normal(0, 3, 5000)            # signed values
    yj = YeoJohnson(lmbda)
    assert np.max(np.abs(yj.inverse(yj.forward(x)) - x)) < 1e-8


def test_log1p_and_logit_roundtrip():
    x = np.array([0.0, 0.1, 1.0, 5.0, 50.0])
    lg = Log1p()
    assert np.max(np.abs(lg.inverse(lg.forward(x)) - x)) < 1e-10
    lo, hi = 0.0, 100.0
    p = np.array([1.0, 25.0, 50.0, 75.0, 99.0])    # interior of (lo,hi)
    logit = Logit(lo, hi)
    assert np.max(np.abs(logit.inverse(logit.forward(p)) - p)) < 1e-8


def test_yeojohnson_reduces_skew():
    rng = np.random.default_rng(1)
    x = rng.gamma(1.5, 2.0, 20000)        # right-skewed (like wind)
    _, lmbda = stats.yeojohnson(x)
    yj = YeoJohnson(float(lmbda))
    assert abs(stats.skew(yj.forward(x))) < abs(stats.skew(x)) * 0.5


def test_transformset_cube_roundtrip():
    meta = synthetic_station_meta(2)
    cube = synthetic_cube(meta, VARS, periods=24 * 120)
    cfg = {
        "temperature_c": {"kind": "gaussian", "bounds": [-40, 55]},
        "wind_speed_ms": {"kind": "positive_skew", "bounds": [0, 75]},
        "pressure_sea_hpa": {"kind": "gaussian", "bounds": [930, 1080]},
        "dew_point_c": {"kind": "gaussian", "bounds": [-45, 40]},
        "humidity_pct": {"kind": "bounded_01", "bounds": [0, 100]},
        "cloud_cover_pct": {"kind": "bounded_01", "bounds": [0, 100]},
        "precip_1h_mm": {"kind": "intermittent", "bounds": [0, 250]},
    }
    tset = transforms.fit(cube, cfg)
    recon = tset.inverse(tset.forward(cube))
    # bounded variables saturate at the logit clip; everything else is exact
    assert np.nanmax(np.abs(recon.values - cube.values)) < 0.5
