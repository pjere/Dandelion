"""DM Phase 3 offline test: the calibration recovers a known heating gradient + threshold."""
from __future__ import annotations

import numpy as np
import pandas as pd
from demand_model.calibration.design import make_design
from demand_model.calibration.fit import _ridge, estimate_thresholds, winter_gradient_gw_per_c
from demand_model.calibration.model import CalibratedModel


def _synthetic_feat(n=24 * 400, rng=None):
    rng = rng or np.random.default_rng(0)
    idx = pd.date_range("2019-01-01", periods=n, freq="h", tz="UTC")
    doy = idx.dayofyear.to_numpy()
    t = 12 - 9 * np.cos(2 * np.pi * doy / 365) + rng.normal(0, 2, n)     # seasonal temp
    feat = pd.DataFrame(index=idx)
    feat["T_nat"] = t
    feat["T_smooth_12h"] = pd.Series(t, index=idx).ewm(halflife=12).mean()
    feat["T_smooth_60h"] = pd.Series(t, index=idx).ewm(halflife=60).mean()
    feat["hour_local"] = idx.hour
    feat["dow"] = idx.dayofweek
    feat["month"] = idx.month
    feat["day_type"] = np.where(idx.dayofweek >= 5, "sat", "tue_thu")
    feat["school_frac"] = 0.0
    feat["GHI_nat"] = 0.0
    feat["trend_years"] = (idx - idx[0]).total_seconds() / (365.25 * 24 * 3600)
    feat["is_covid"] = 0.0
    feat["is_sobriety"] = 0.0
    return feat


def test_calibration_recovers_gradient():
    rng = np.random.default_rng(1)
    feat = _synthetic_feat(rng=rng)
    tau = 15.0
    hdd = np.clip(tau - feat["T_smooth_60h"], 0, None)
    load = 40000 + 2400 * hdd + rng.normal(0, 300, len(feat))     # known -2.4 GW/°C heating
    feat = feat.assign()  # noop keep

    # threshold estimation lands near the true knee
    daily = feat[["T_smooth_60h", "T_smooth_12h"]].resample("1D").mean()
    daily["load"] = pd.Series(load, index=feat.index).resample("1D").mean()
    th, tc = estimate_thresholds(daily["load"], daily["T_smooth_60h"], daily["T_smooth_12h"])
    assert 13.5 <= th <= 16.5

    X, groups = make_design(feat, th, tc)
    coef, intercept = _ridge(X.to_numpy(), load, alpha=2.0)
    model = CalibratedModel(intercept=intercept, coef=pd.Series(coef, index=X.columns),
                            groups=groups, tau_heat=th, tau_cool=tc, tau_cold=2.0, halflives_h=[12, 60])
    grad = winter_gradient_gw_per_c(model, feat)
    assert -2.9 < grad < -1.9        # recovers ~ -2.4 GW/°C
    # separable components sum to the full prediction
    comp = model.components(feat).sum(axis=1)
    assert np.allclose(comp.to_numpy(), model.predict(feat).to_numpy(), atol=1e-6)
