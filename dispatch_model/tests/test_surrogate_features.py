"""Surrogate feature layer: the vectorised supply curve must equal a brute-force reference, and the
no-leakage guard must actually fire. DB-free and fast."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from dispatch_model.surrogate import dataset as ds
from dispatch_model.surrogate.features import (
    add_neighbour_context,
    assert_no_leakage,
    curve_knots,
    srmc_at,
    supply_curves,
    zone_features,
)


def _brute_srmc_at(cap_row, srmc_row, load):
    """Reference: sort by cost, cumulate, return the SRMC of the tech serving `load`."""
    order = sorted(range(len(cap_row)), key=lambda i: srmc_row[i])
    cum = 0.0
    for i in order:
        cum += cap_row[i]
        if cum >= load:
            return srmc_row[i]
    return srmc_row[order[-1]]


def test_supply_curve_matches_bruteforce():
    rng = np.random.default_rng(0)
    cap = rng.uniform(100, 5000, size=(200, 7))
    srmc = rng.uniform(1, 300, size=(200, 7))
    load = rng.uniform(0, cap.sum(axis=1).max(), size=200)
    cum, ss = supply_curves(cap, srmc)
    got = srmc_at(cum, ss, load)
    want = [_brute_srmc_at(cap[i], srmc[i], load[i]) for i in range(200)]
    assert np.allclose(got, want)


def test_supply_curve_is_monotone_and_cumulates_total():
    cap = np.array([[1000.0, 2000.0, 500.0]])
    srmc = np.array([[50.0, 10.0, 200.0]])
    cum, ss = supply_curves(cap, srmc)
    assert np.all(np.diff(ss[0]) >= 0)                 # sorted cheapest first
    assert ss[0].tolist() == [10.0, 50.0, 200.0]
    assert cum[0].tolist() == [2000.0, 3000.0, 3500.0]


def test_missing_tech_contributes_nothing():
    """A tech absent from the zone (NaN capacity / NaN SRMC) must not shift the curve."""
    cap = np.array([[1000.0, np.nan]])
    srmc = np.array([[50.0, np.nan]])
    cum, ss = supply_curves(cap, srmc)
    assert cum[0, -1] == 1000.0
    assert srmc_at(cum, ss, np.array([500.0]))[0] == 50.0


def test_load_beyond_stack_returns_dearest():
    cap = np.array([[1000.0, 1000.0]])
    srmc = np.array([[20.0, 90.0]])
    cum, ss = supply_curves(cap, srmc)
    assert srmc_at(cum, ss, np.array([1e6]))[0] == 90.0


def test_curve_knots_are_monotone_in_quantile():
    rng = np.random.default_rng(1)
    cum, ss = supply_curves(rng.uniform(100, 3000, (50, 6)), rng.uniform(1, 200, (50, 6)))
    k = curve_knots(cum, ss)
    vals = np.vstack([k[f"curve_q{q}"] for q in (10, 30, 50, 70, 90)])
    assert np.all(np.diff(vals, axis=0) >= -1e-9)      # dearer as you go up the stack


def _toy(n=48, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2019-01-01", periods=n, freq="h", tz="UTC")
    techs = ["nuclear", "gas", "coal"]
    cap = pd.DataFrame(rng.uniform(1000, 4000, (n, 3)), index=idx, columns=techs)
    srmc = pd.DataFrame(rng.uniform(5, 150, (n, 3)), index=idx, columns=techs)
    prices = pd.DataFrame({"gas": 30.0, "coal": 12.0, "oil": 60.0, "co2": 80.0}, index=idx)
    load = pd.Series(rng.uniform(2000, 8000, n), index=idx)
    res = pd.Series(rng.uniform(0, 2000, n), index=idx)
    return idx, load, res, cap, srmc, prices


def test_zone_features_shape_and_no_reserve_margin():
    f = zone_features(*_toy())
    assert len(f) == 48
    # 1 - tightness carries no information; it must not have crept back in
    assert "reserve_margin" not in f.columns
    assert {"tightness", "res_share", "srmc_at_residual", "sp_clean_spark", "d_srmc_1"} <= set(f.columns)
    assert f["srmc_at_residual"].notna().all()


def test_no_leakage_guard_fires_on_outcome_columns():
    f = zone_features(*_toy())
    assert_no_leakage(f)                                # clean frame passes
    f["price_observed"] = 1.0
    with pytest.raises(ValueError, match="must not be features"):
        assert_no_leakage(f)


def test_neighbour_context_excludes_self():
    a, b = zone_features(*_toy(seed=1)), zone_features(*_toy(seed=2))
    out = add_neighbour_context({"FR": a, "BE": b}, ["FR", "BE"])
    # FR's neighbour mean is BE's tightness alone, never FR's own
    assert np.allclose(out["FR"]["nb_tight_mean"], b["tightness"])


def test_feature_columns_exclude_every_label_derived_column():
    """`abs_err_eur` and `margin_eur` are computed *from the observed price*; if either reached the design
    matrix the model would be reading its own answer."""
    panel = pd.DataFrame({
        "timestamp_utc": [1], "zone": ["FR"], "year": [2019], "usable": [True],
        "setting_zone": ["FR"], "tranche_tech": ["gas"], "regime": ["thermal"], "confidence": [1.0],
        "srmc_implied": [50.0], "price_observed": [52.0], "abs_err_eur": [2.0],
        "margin_eur": [5.0], "agrees_price_match": [True], "tightness": [0.7]})
    assert ds.feature_columns(panel) == ["tightness"]


def test_zone_dummies_are_features_and_one_hot():
    panel = pd.DataFrame({"zone": ["FR", "BE", "FR"], "tightness": [0.1, 0.2, 0.3]})
    out = ds.with_zone_dummies(panel)
    assert out["z_FR"].tolist() == [1.0, 0.0, 1.0]
    assert out["z_BE"].tolist() == [0.0, 1.0, 0.0]
    assert {"z_FR", "z_BE"} <= set(ds.feature_columns(out))


def test_build_panel_drops_ch_and_keeps_confidence_weight():
    idx, *_ = _toy(24)
    feats = {z: zone_features(*_toy(24, seed=i)) for i, z in enumerate(["FR", "CH"])}
    lab = pd.concat([
        pd.DataFrame({"timestamp_utc": idx, "zone": z, "setting_zone": z, "tranche_tech": "gas",
                      "regime": "thermal", "confidence": 0.75, "srmc_implied": 50.0,
                      "price_observed": 52.0})
        for z in ("FR", "CH")], ignore_index=True)
    panel = ds.build_panel(feats, lab)
    assert set(panel["zone"]) == {"FR"}                 # CH excluded by default
    assert panel["usable"].all()
    assert "confidence" in panel.columns
    assert "confidence" not in ds.feature_columns(panel)


def test_split_is_temporal_not_random():
    idx = pd.date_range("2019-01-01", periods=10, freq="h", tz="UTC")
    idx24 = pd.date_range("2024-01-01", periods=10, freq="h", tz="UTC")
    panel = pd.DataFrame({
        "timestamp_utc": list(idx) + list(idx24), "zone": "FR",
        "year": [2019] * 10 + [2024] * 10, "usable": True,
        "tranche_tech": "gas", "confidence": 1.0, "tightness": 0.5})
    tr, ho = ds.split(panel)
    assert set(tr["year"]) == {2019} and set(ho["year"]) == {2024}
