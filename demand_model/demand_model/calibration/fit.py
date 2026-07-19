"""Phase 3 — estimate thresholds, fit the ridge, backtest, extract the winter gradient."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import Config
from .design import make_design
from .model import CalibratedModel


def _ridge(X: np.ndarray, y: np.ndarray, alpha: float) -> tuple[np.ndarray, float]:
    """Closed-form ridge with an unpenalised intercept."""
    n, p = X.shape
    Xa = np.column_stack([np.ones(n), X])
    D = np.eye(p + 1) * alpha
    D[0, 0] = 0.0                                    # do not penalise intercept
    beta = np.linalg.solve(Xa.T @ Xa + D, Xa.T @ y)
    return beta[1:], float(beta[0])


def estimate_thresholds(daily_load: pd.Series, t_slow: pd.Series, t_fast: pd.Series) -> tuple[float, float]:
    """Grid-search the heating/cooling knees by daily-scale fit (data-driven, not imposed)."""
    dl = daily_load.to_numpy()
    month = pd.get_dummies(daily_load.index.month, drop_first=True).to_numpy(dtype=float)
    best, best_rss = (15.0, 20.0), np.inf
    for th in np.arange(13, 17.1, 0.5):
        hdd = np.clip(th - t_slow.to_numpy(), 0, None)
        for tc in np.arange(18, 23.1, 0.5):
            cdd = np.clip(t_fast.to_numpy() - tc, 0, None)
            X = np.column_stack([np.ones_like(dl), hdd, cdd, month])
            beta, *_ = np.linalg.lstsq(X, dl, rcond=None)
            rss = float(np.sum((dl - X @ beta) ** 2))
            if rss < best_rss:
                best, best_rss = (float(th), float(tc)), rss
    return best


def _mape(y, yhat) -> float:
    return float(np.mean(np.abs((y - yhat) / y)) * 100)


def winter_gradient_gw_per_c(model: CalibratedModel, feat: pd.DataFrame) -> float:
    """Empirical dLoad/dT (GW/°C) over cold hours: predict at the observed slow temperature and at
    +1°C, difference the heat component."""
    cold = feat[feat["T_smooth_60h"] < 8].copy()
    if cold.empty:
        return np.nan
    warm = cold.copy()
    # sustained +1°C: ALL temperature-derived inputs shift together (RTE-style gradient)
    for c in ("T_nat", "T_smooth_60h", "T_smooth_12h", "T_lag_d1", "T_lag_d2"):
        if c in warm:
            warm[c] = warm[c] + 1.0
    d = (model.predict(warm) - model.predict(cold)).mean()
    return float(d / 1000.0)                          # MW/°C -> GW/°C (negative)


def calibrate(config: Config, feat: pd.DataFrame, load: pd.DataFrame) -> CalibratedModel:
    ec = config.section("effective_temperature")
    cc = config.section("calibration")
    df = feat.join(load.set_index("timestamp_utc")["load_mw"], how="inner").dropna(
        subset=["load_mw", "T_smooth_60h", "T_smooth_12h"])

    # estimate thresholds on daily data
    daily = df[["load_mw", "T_smooth_60h", "T_smooth_12h"]].resample("1D").mean().dropna()
    tau_heat, tau_cool = estimate_thresholds(daily["load_mw"], daily["T_smooth_60h"], daily["T_smooth_12h"])

    X, groups = make_design(df, tau_heat, tau_cool)
    ok = X.notna().all(axis=1) & df["load_mw"].notna()      # drop warm-up rows (lagged temps NaN)
    X, df = X[ok], df[ok]
    y = df["load_mw"].to_numpy()

    # hold out the backtest year(s)
    holdout = set(cc.get("holdout_years", []))
    is_hold = df.index.year.isin(holdout)
    Xtr, ytr = X.to_numpy()[~is_hold], y[~is_hold]
    coef, intercept = _ridge(Xtr, ytr, alpha=4.0)
    model = CalibratedModel(
        intercept=intercept, coef=pd.Series(coef, index=X.columns), groups=groups,
        tau_heat=tau_heat, tau_cool=tau_cool, tau_cold=2.0,
        halflives_h=ec["smoothing_halflives_h"],
    )

    yhat = model.predict(df)
    metrics = {
        "n_train": int((~is_hold).sum()), "n_holdout": int(is_hold.sum()),
        "mape_in_sample": round(_mape(y[~is_hold], yhat.to_numpy()[~is_hold]), 3),
        "tau_heat": tau_heat, "tau_cool": tau_cool,
        "winter_gradient_gw_per_c": round(winter_gradient_gw_per_c(model, df), 3),
    }
    if is_hold.any():
        metrics["mape_holdout"] = round(_mape(y[is_hold], yhat.to_numpy()[is_hold]), 3)
        metrics["holdout_bias_pct"] = round(float((yhat.to_numpy()[is_hold] - y[is_hold]).mean() /
                                                   y[is_hold].mean() * 100), 3)
    model.metrics = metrics
    return model
