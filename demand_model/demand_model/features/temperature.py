"""Phase 2 — national effective temperature and thermosensitive variables (§5).

T_nat = weighted mean of station temperatures (population/consumption weights from the workbook,
equal-weight fallback). Building thermal inertia is captured by exponentially-smoothed T_nat at
two time constants (~12 h and ~48–72 h) plus lagged daily means (D-1, D-2). Heating/cooling degree
variables are piecewise-linear so the gradients and thresholds are read off the data in calibration
(a cold-tail term lets the winter gradient steepen in extreme cold — HP COP degradation etc.).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import Config


def station_weights(config: Config, station_ids: list[str]) -> pd.Series:
    """Normalised station weights for T_nat. Reads the workbook 'weights' sheet if populated,
    else falls back to equal weights (flagged)."""
    w = pd.Series(1.0, index=[str(s) for s in station_ids])
    try:
        from ..io.assumptions import load_assumptions
        wb = load_assumptions(config.resolve(config.section("assumptions")["workbook"]))
        ws = wb["weights"]
        if not ws.empty and "_FILL_" not in set(ws["station_id"].astype(str)):
            ws = ws[ws["station_id"].astype(str).isin(w.index)]
            if not ws.empty:
                w = ws.set_index(ws["station_id"].astype(str))["weight"].reindex(w.index).fillna(0.0)
    except Exception:
        pass
    return w / w.sum()


def national_temperature(weather: pd.DataFrame, weights: pd.Series) -> pd.Series:
    """Weighted national 2 m temperature (renormalising weights over stations present each hour)."""
    wide = weather.pivot_table(index="timestamp_utc", columns="station_id",
                               values="temperature_c", aggfunc="mean")
    wide = wide.reindex(columns=[c for c in weights.index if c in wide.columns])
    w = weights.reindex(wide.columns).to_numpy()
    x = wide.to_numpy()
    mask = ~np.isnan(x)
    wsum = (mask * w).sum(axis=1)
    num = np.nansum(np.where(mask, x * w, 0.0), axis=1)
    return pd.Series(np.where(wsum > 0, num / np.where(wsum > 0, wsum, 1), np.nan),
                     index=wide.index, name="T_nat")


def smoothed_temperatures(t_nat: pd.Series, halflives_h: list[int], lags_d: list[int]) -> pd.DataFrame:
    """Exponentially-smoothed T_nat (thermal inertia) + lagged daily means."""
    out = pd.DataFrame({"T_nat": t_nat})
    for hl in halflives_h:
        out[f"T_smooth_{hl}h"] = t_nat.ewm(halflife=hl, adjust=False).mean()
    daily = t_nat.resample("1D").mean()                 # daily means on UTC midnights
    day_key = t_nat.index.floor("1D")                   # each hour -> its day start
    for d in lags_d:
        lagged = daily.shift(d)                          # daily mean d days earlier
        out[f"T_lag_d{d}"] = lagged.reindex(day_key).to_numpy()
    return out


def heating_cooling(t_smooth: pd.Series, tau_heat: float, tau_cool: float,
                    tau_cold: float = 2.0) -> pd.DataFrame:
    """Piecewise-linear thermosensitive basis: heating (HDD), a cold-tail term (steeper gradient
    in extreme cold), and cooling (CDD). Thresholds default here; calibration re-estimates them."""
    t = t_smooth.to_numpy()
    return pd.DataFrame({
        "HDD": np.clip(tau_heat - t, 0, None),          # heating degree
        "HDD_cold": np.clip(tau_cold - t, 0, None),     # extra cold-tail slope (T < ~2°C)
        "CDD": np.clip(t - tau_cool, 0, None),          # cooling degree
    }, index=t_smooth.index)
