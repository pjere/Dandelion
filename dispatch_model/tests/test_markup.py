"""Markup layer: feature design, that the OLS recovers a known wedge, and that apply is bounded."""
from __future__ import annotations

import numpy as np
import pandas as pd
from dispatch_model.markup import _feature_names, _features, apply_markup, fit_markup


def _panel(n=4000, seed=0):
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2019-01-01", periods=n, freq="h", tz="UTC")
    demand = 50000 + 15000 * np.sin(2 * np.pi * ts.hour / 24) + rng.normal(0, 3000, n)
    res = np.clip(rng.uniform(0, 25000, n), 0, None)
    firm = 90000.0
    smc = rng.uniform(-10, 120, n)
    df = pd.DataFrame({"zone": "FR", "timestamp_utc": ts, "smc": smc, "demand": demand,
                       "musttake_res": res, "firm_cap": firm})
    # a *known* structural wedge: proportional markup + convex tightness scarcity
    tight = np.clip((demand - res) / firm, 0, 1.6)
    wedge = 2.0 + 0.15 * smc + 40.0 * tight ** 2
    df["observed"] = smc + wedge + rng.normal(0, 2.0, n)
    return df


def test_feature_matrix_shape():
    df = _panel(100)
    X = _features(df)
    assert X.shape == (100, len(_feature_names()))
    assert np.isfinite(X).all()


def test_fit_recovers_wedge_and_beats_raw_smc():
    m = fit_markup(_panel())
    d = m["diagnostics"]["FR"]
    assert d["rmse_spot"] < 0.3 * d["rmse_smc"]     # the markup must cut price error substantially
    assert d["r2_spot"] > 0.95                       # known low-noise wedge → near-perfect recovery


def test_apply_is_bounded_and_adds_positive_markup_on_average():
    df = _panel()
    m = fit_markup(df)
    smc = df.set_index("timestamp_utc")["smc"]
    drv = df[["timestamp_utc", "demand", "musttake_res", "firm_cap"]].set_index("timestamp_utc").reset_index()
    drv.index = smc.index
    spot = apply_markup(m, "FR", smc, drv, floor=-500, voll=4000)
    assert spot.between(-500, 4000).all()
    assert spot.mean() > smc.mean()                  # wedge is net positive here


def test_markup_does_not_collapse_when_extrapolated_to_a_2040_like_regime():
    """The projectability guard. A 2040 year breaks the training year's price↔tightness correlation (high SMC
    from gas/CO2, LOW tightness from abundant RES) — a combination 2019 never contains. An unconstrained fit
    happily extrapolates a wedge that *shrinks* with price (real symptom: IT-North SMC €126 → "spot" €58).
    The sign constraints + envelope clamping must keep the wedge from going sharply negative there."""
    df = _panel()
    m = fit_markup(df)
    n = 500
    ts = pd.date_range("2040-01-01", periods=n, freq="h", tz="UTC")
    # 2040-like: SMC far above the training range, RES share far above it, tightness far BELOW it
    smc = pd.Series(np.full(n, 250.0), index=ts)
    drv = pd.DataFrame({"timestamp_utc": ts, "demand": np.full(n, 50000.0),
                        "musttake_res": np.full(n, 40000.0), "firm_cap": np.full(n, 90000.0)})
    spot = apply_markup(m, "FR", smc, drv)
    assert (spot >= smc - 5).all()      # wedge must not collapse the price below SMC out of envelope
    assert spot.max() < 4000


def test_unknown_zone_falls_back_to_clipped_smc():
    m = fit_markup(_panel())
    smc = pd.Series([50.0, -600.0, 5000.0])
    drv = pd.DataFrame({"timestamp_utc": pd.date_range("2019-01-01", periods=3, freq="h", tz="UTC"),
                        "demand": [5e4] * 3, "musttake_res": [1e4] * 3, "firm_cap": [9e4] * 3})
    out = apply_markup(m, "ZZ", smc, drv, floor=-500, voll=4000)
    assert list(out) == [50.0, -500.0, 4000.0]
