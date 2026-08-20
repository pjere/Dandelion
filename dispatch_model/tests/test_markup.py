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
    m = fit_markup(_panel(), shrink=1.0)             # un-shrunk : teste la machinerie de fit elle-meme
    d = m["diagnostics"]["FR"]
    assert d["rmse_spot"] < 0.3 * d["rmse_smc"]     # the markup must cut price error substantially
    assert d["r2_spot"] > 0.95                       # known low-noise wedge → near-perfect recovery


def test_shrink_scales_the_wedge_toward_zero():
    """Le rétrécissement (defaut 0,5) divise le wedge applique par deux, niveau ET pente. Mesure hors
    echantillon : le wedge plein calibre crise-inclus surcorrige le SMC ameliore ; le demi-wedge garde la
    MAE au niveau du SMC brut en coupant quand meme l'erreur de niveau de moitie."""
    df = _panel()
    smc = df.set_index("timestamp_utc")["smc"]
    drv = df[["timestamp_utc", "demand", "musttake_res", "firm_cap"]].set_index("timestamp_utc").reset_index()
    drv.index = smc.index
    full = apply_markup(fit_markup(df, shrink=1.0), "FR", smc, drv) - smc
    half = apply_markup(fit_markup(df, shrink=0.5), "FR", smc, drv) - smc
    assert np.allclose(half.to_numpy(), 0.5 * full.to_numpy(), atol=1e-6)
    assert fit_markup(df)["shrink"] == 0.5           # 0,5 est le defaut de production


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


# ---------------------------------------------------------------------------------------------------
# The tightness response must be non-decreasing, and a failed dispatch must not reach the fit
# ---------------------------------------------------------------------------------------------------
# Both properties below were violated in the model shipped 2026-08, and neither was visible in the fit
# diagnostics: the sign pathology hid inside a collinear pair, and the bad zone-year passed a median and a
# correlation test because 100 VOLL hours out of 8735 move neither.


def _panel_falling_wedge(n=4000, seed=3):
    """A panel whose LEAST-SQUARES answer is a wedge that falls with tightness.

    Unconstrained (or constrained only on `tight`), the fit is free to express this through `tight_sq`,
    which is what GB did. The economics say a wedge must not shrink as the system tightens, so the fit is
    required to refuse this data rather than reproduce it."""
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2019-01-01", periods=n, freq="h", tz="UTC")
    demand = 50000 + 15000 * np.sin(2 * np.pi * ts.hour / 24) + rng.normal(0, 3000, n)
    res = np.clip(rng.uniform(0, 25000, n), 0, None)
    firm = 90000.0
    smc = rng.uniform(-10, 120, n)
    tight = np.clip((demand - res) / firm, 0, 1.6)
    df = pd.DataFrame({"zone": "FR", "timestamp_utc": ts, "smc": smc, "demand": demand,
                       "musttake_res": res, "firm_cap": firm})
    df["observed"] = smc + (60.0 - 120.0 * tight ** 2) + rng.normal(0, 2.0, n)
    return df


def test_tight_sq_is_sign_constrained():
    """`tight` alone is not enough: it is 0.96-0.99 collinear with `tight_sq`, so a bound on one is
    satisfied by pinning it to zero and loading the other."""
    from dispatch_model.markup import _SIGN_LB
    assert _SIGN_LB.get("tight_sq") == 0.0
    m = fit_markup(_panel_falling_wedge(), shrink=1.0)
    names = _feature_names()[1:]
    beta = dict(zip(names, m["coef"]["FR"]["beta_z"]))
    assert beta["tight_sq"] >= -1e-9, f"tight_sq went negative: {beta['tight_sq']:.3f}"
    assert beta["tight"] >= -1e-9


def test_the_fitted_wedge_never_falls_with_tightness():
    """The property the constraints exist for, asserted on the wedge itself rather than on coefficients —
    a future reparametrisation must keep this even if the feature set changes."""
    from dispatch_model.markup import _driver_bounds, _features as _F, _predict
    g = _panel_falling_wedge()
    m = fit_markup(g, shrink=1.0)
    mz = m["coef"]["FR"]
    lo, hi = _driver_bounds(g)["tight"]
    grid = np.linspace(lo, hi, 25)
    # res_share must be held CONSTANT along the sweep, or this measures the res_share slope as well:
    # tight = (demand - res)/firm, so raising demand at fixed res raises tight and lowers res = res/demand
    # at the same time. Fixing res = r*demand gives tight = demand*(1-r)/firm, a clean tightness axis.
    firm, r = 90000.0, 0.20
    demand = grid * firm / (1.0 - r)
    probe = pd.DataFrame({"timestamp_utc": pd.to_datetime(["2019-06-15 12:00"] * len(grid), utc=True),
                          "smc": 60.0, "firm_cap": firm, "musttake_res": r * demand,
                          "demand": demand})
    wedge = _predict(mz, _F(probe, mz["bounds"]))
    assert np.all(np.diff(wedge) >= -1e-6), (
        f"wedge falls with tightness: {wedge.round(2).tolist()}")


def test_panel_rejects_a_zone_year_whose_dispersion_is_absurd(monkeypatch):
    """The zone-year built here is RIGHT on both older tests and wrong only on dispersion: same median,
    near-perfect correlation, but a modelled swing 3.5x the market's.

    That is GB's bulk defect, isolated. (Its VOLL spikes are a second symptom, but spikes big enough to
    blow the standard deviation also drag the correlation under the existing 0.2 bar, so they would be
    caught by test (b) and would not prove this criterion adds anything.)"""
    from dispatch_model import markup as M

    n = 2000
    rng = np.random.default_rng(11)
    ts = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    base = 80.0 + rng.normal(0, 12.0, n)         # a real market has dispersion; a constant one is degenerate
    obs = pd.Series(base, index=ts)
    smc = pd.Series(80.0 + 3.5 * (base - 80.0) + rng.normal(0, 1.0, n), index=ts)
    drv = pd.DataFrame({"timestamp_utc": ts, "demand": 50000.0,
                        "musttake_res": 10000.0, "firm_cap": 90000.0})

    monkeypatch.setattr(M, "_year_smc", lambda c, y: pd.DataFrame({"Z": smc.to_numpy()}, index=ts))
    monkeypatch.setattr(M, "zone_drivers", lambda c, y: {"Z": drv.copy()})
    monkeypatch.setattr("dispatch_model.rolling.backtest._observed_prices", lambda c, y, z: {"Z": obs})
    monkeypatch.setattr("dispatch_model.rolling.assemble.modelled_zones", lambda c: ["Z"])

    kept = M.build_panel(None, [2024], max_std_ratio=float("inf"))
    assert len(kept) == n, "inf threshold must reproduce the old panel"
    assert M.build_panel(None, [2024]).empty, "the VOLL-tail zone-year should have been excluded"
