"""Phase 3 — onshore wind conversion (§5.2).

Hub-height wind (from the D1 transfer) → a **smoothed aggregate power curve**. A single-turbine curve
is  0 below cut-in, ∝ v³ up to rated (rated speed set by the cohort's specific power), flat to cut-out,
0 above. Aggregating hundreds of geographically-spread turbines smears every knee, so the fleet curve
is the single-turbine curve convolved with a Gaussian of width ``smoothing_ms`` (fitted in Phase 4,
not assumed). Output is a per-unit capacity factor.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

_RHO, _CP = 1.225, 0.45            # air density, power coefficient (for the rated-speed relation)
_V = np.arange(0.0, 45.0 + 1e-9, 0.1)   # wind-speed grid (m/s)


def rated_speed(specific_power_w_m2: float) -> float:
    """v_rated from specific power: P_rated/A = ½ρ·Cp·v_rated³."""
    return float((specific_power_w_m2 / (0.5 * _RHO * _CP)) ** (1.0 / 3.0))


def _single_turbine(specific_power: float, cut_in: float, cut_out: float) -> np.ndarray:
    v_rated = rated_speed(specific_power)
    p = np.zeros_like(_V)
    rise = (_V >= cut_in) & (_V < v_rated)
    p[rise] = (_V[rise] ** 3 - cut_in ** 3) / (v_rated ** 3 - cut_in ** 3)
    p[(_V >= v_rated) & (_V < cut_out)] = 1.0
    return np.clip(p, 0.0, 1.0)


def aggregate_power_curve(specific_power: float = 300.0, smoothing_ms: float = 2.0,
                          cut_in: float = 3.0, cut_out: float = 25.0) -> np.ndarray:
    """Fleet curve on the ``_V`` grid: single-turbine curve convolved with a Gaussian (spatial smear)."""
    single = _single_turbine(specific_power, cut_in, cut_out)
    sigma = max(smoothing_ms / 0.1, 1e-6)                            # grid points
    half = int(np.ceil(4 * sigma))
    x = np.arange(-half, half + 1)
    kern = np.exp(-0.5 * (x / sigma) ** 2); kern /= kern.sum()
    return np.clip(np.convolve(single, kern, mode="same"), 0.0, 1.0)


def onshore_cf(wind: pd.Series, specific_power: float = 300.0, smoothing_ms: float = 2.0,
               availability: float = 0.96, icing_derate: float = 0.0,
               cut_in: float = 3.0, cut_out: float = 25.0) -> pd.Series:
    """Per-unit onshore capacity factor from a hub-height wind series."""
    curve = aggregate_power_curve(specific_power, smoothing_ms, cut_in, cut_out)
    cf = np.interp(wind.to_numpy(), _V, curve)
    cf = cf * availability * (1.0 - icing_derate)
    return pd.Series(np.clip(cf, 0.0, 1.0), index=wind.index, name="onshore_cf")
