"""Phase 2 — station-10 m → ERA5-100 m wind transfer (D1).

The 45 stations measure 10 m over land at meteorology-chosen sites; wind farms sit at hub height
(~100 m) at wind-chosen sites, and offshore is uncovered. Rather than a physics power-law
extrapolation (large, site-dependent bias), we estimate a **monotone log-linear transfer with lag
terms** mapping station 10 m wind → ERA5 100 m wind on the historical overlap, then apply it to the
synthetic station draws. The same machinery maps a coastal station → an offshore farm's ERA5-100 m
grid point (coastal→offshore correlation model). ERA5 supplies the hub-height/offshore physics
without reopening weathergen.

    log(w100 + ε) = a + b·log(w10 + ε) + Σ_k c_k·log(w10[t−k] + ε)

b ≈ the effective shear exponent; the lag terms absorb boundary-layer stability/persistence.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

_EPS = 0.1                          # m/s, avoids log(0) at calm


@dataclass
class WindTransfer:
    region: str
    lags: tuple[int, ...]
    coef: np.ndarray                # [intercept, b_0, c_lag1, c_lag2, ...]
    resid_std: float                # std of log residual (for the stochastic layer prior)
    r2: float

    def _design(self, w10: pd.Series) -> tuple[np.ndarray, pd.Index]:
        lw = np.log(w10.to_numpy() + _EPS)
        cols = [np.ones_like(lw), lw]
        base = pd.Series(lw, index=w10.index)
        for k in self.lags:
            cols.append(base.shift(k).to_numpy())
        X = np.column_stack(cols)
        ok = ~np.isnan(X).any(axis=1)
        return X[ok], w10.index[ok]

    def predict(self, w10: pd.Series) -> pd.Series:
        X, idx = self._design(w10)
        w100 = np.exp(X @ self.coef) - _EPS
        return pd.Series(np.clip(w100, 0.0, None), index=idx, name="wind100_ms")


def fit_wind_transfer(w10: pd.Series, w100: pd.Series, region: str = "FR",
                      lags: tuple[int, ...] = (1, 2)) -> WindTransfer:
    """OLS fit of the log-linear transfer on the aligned historical overlap."""
    df = pd.concat([w10.rename("w10"), w100.rename("w100")], axis=1).dropna().sort_index()
    lw10 = np.log(df["w10"].to_numpy() + _EPS)
    y = np.log(df["w100"].to_numpy() + _EPS)
    base = pd.Series(lw10, index=df.index)
    cols = [np.ones_like(lw10), lw10] + [base.shift(k).to_numpy() for k in lags]
    X = np.column_stack(cols)
    ok = ~np.isnan(X).any(axis=1)
    X, y = X[ok], y[ok]
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coef
    ss_res, ss_tot = float(np.sum(resid ** 2)), float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return WindTransfer(region=region, lags=lags, coef=coef,
                        resid_std=float(np.std(resid)), r2=r2)


def apply_wind_transfer(model: WindTransfer, w10: pd.Series) -> pd.Series:
    """Map station 10 m wind → hub-height/offshore 100 m wind with a fitted transfer."""
    return model.predict(w10)


def transfer_quality_vs_era5(station_to_prod_r2: float, era5_to_prod_r2: float,
                             tol: float = 0.05) -> str:
    """§2.A cross-check verdict: flag if station→production is materially worse than ERA5→production."""
    gap = era5_to_prod_r2 - station_to_prod_r2
    if gap > tol:
        return (f"FLAG: station→production R² ({station_to_prod_r2:.3f}) is {gap:.3f} below "
                f"ERA5→production ({era5_to_prod_r2:.3f}) — consider co-generating 100 m wind in step (ii).")
    return f"OK: station transfer within {tol:.2f} R² of ERA5 (Δ={gap:+.3f})."
