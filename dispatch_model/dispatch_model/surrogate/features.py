"""Features for the marginal-tranche model — structural state, fuel economics, calendar and lags.

**Two hard rules, both about not fooling ourselves.**

*No leakage.* Every feature here must be computable at **projection** time, from the same inputs the LP
consumes: residual load, available capacity, exogenous fuel/ETS prices, NTC, hydro budget, calendar.
Observed price and observed generation are therefore **forbidden** — the Δ-generation signal that derives
the *label* (see `labels.py`) does not exist in 2046 and can never be an input. The previous hour's
marginal tranche is likewise not a feature: it is a model *output*, and its temporal dependence belongs in
the CRF transition matrix, not in the design matrix.

*Ratios, not levels.* A model trained on 2019-24 must recognise a 2046 hour. Absolute megawatts never
recur; **tightness** (residual load over firm capacity), **RES share** and **reserve margin** do. The same
argument applies to fuel: what fixes the merit *order* is the clean spark/dark spread and the gas-coal
switching price, not the price level — so both go in, and the ordering signals are what generalise.

The strongest single feature is `srmc_at_residual`: where residual load falls on the zone's own monotone
supply curve, i.e. the single-zone merit-order price. The model's real job is then to learn the
*corrections* to it — market coupling, ramp/commitment limits, scarcity — which is a far better-posed
problem than predicting a price level from scratch.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .tranches import fuel_spreads

#: capacity quantiles at which the supply curve is sampled (a fixed-length monotone descriptor)
CURVE_Q = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95)
LAGS = (1, 2, 3, 6, 12, 24)          # hours: ramp/commitment memory + a daily cycle
EPS = 1e-9


def supply_curves(cap: np.ndarray, srmc: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Merit order for **all hours at once**: sort each row by SRMC and cumulate capacity.

    Vectorised over time deliberately — this runs per hour, per zone, per Monte-Carlo draw, so a Python
    loop here would dominate the surrogate's runtime and defeat its entire purpose. Missing capacity
    becomes 0 MW and missing SRMC +inf, so absent techs sort to the end and contribute nothing.
    """
    c = np.nan_to_num(np.asarray(cap, float), nan=0.0)
    s = np.where(np.isfinite(srmc), srmc, np.inf)
    order = np.argsort(s, axis=1, kind="stable")
    ss = np.take_along_axis(s, order, axis=1)
    cum = np.cumsum(np.take_along_axis(c, order, axis=1), axis=1)
    return cum, ss


def _row_interp(cum: np.ndarray, ss: np.ndarray, target: np.ndarray) -> np.ndarray:
    """Per-row linear interpolation of the (monotone) curve at one target per row."""
    j = np.clip((cum < target[:, None]).sum(axis=1), 0, cum.shape[1] - 1)
    r = np.arange(len(cum))
    x1, y1 = cum[r, j], ss[r, j]
    jm = np.maximum(j - 1, 0)
    x0, y0 = np.where(j > 0, cum[r, jm], 0.0), np.where(j > 0, ss[r, jm], ss[r, 0])
    w = np.where(x1 > x0, (target - x0) / np.where(x1 > x0, x1 - x0, 1.0), 0.0)
    return np.where(np.isfinite(y1), y0 + np.clip(w, 0, 1) * (y1 - y0), y0)


def curve_knots(cum: np.ndarray, ss: np.ndarray, q=CURVE_Q) -> dict[str, np.ndarray]:
    """SRMC at fixed fractions of total available capacity — comparable across years and fleets."""
    total = cum[:, -1]
    return {f"curve_q{int(100 * x)}": _row_interp(cum, ss, x * total) for x in q}


def srmc_at(cum: np.ndarray, ss: np.ndarray, load: np.ndarray) -> np.ndarray:
    """SRMC of the tech serving `load` on the merit order — the single-zone marginal price.

    Load beyond the top of the stack returns the dearest SRMC: scarcity territory, where the LP would be
    pricing at VoLL and the surrogate must defer rather than guess.
    """
    ld = np.nan_to_num(np.asarray(load, float), nan=0.0)
    j = np.clip((cum < ld[:, None]).sum(axis=1), 0, cum.shape[1] - 1)
    out = ss[np.arange(len(cum)), j]
    finite = np.where(np.isfinite(ss), ss, -np.inf).max(axis=1)
    return np.where(np.isfinite(out), out, finite)


def _calendar(idx: pd.DatetimeIndex) -> pd.DataFrame:
    h, m, dow = idx.hour.to_numpy(float), idx.month.to_numpy(float), idx.dayofweek.to_numpy()
    return pd.DataFrame({
        "sin_h": np.sin(2 * np.pi * h / 24), "cos_h": np.cos(2 * np.pi * h / 24),
        "sin_2h": np.sin(4 * np.pi * h / 24), "cos_2h": np.cos(4 * np.pi * h / 24),
        "sin_m": np.sin(2 * np.pi * m / 12), "cos_m": np.cos(2 * np.pi * m / 12),
        "weekend": (dow >= 5).astype(float),
    }, index=idx)


def zone_features(idx: pd.DatetimeIndex, residual_load: pd.Series, res_pot: pd.Series,
                  cap_by_tech: pd.DataFrame, srmc_by_tech: pd.DataFrame,
                  prices: pd.DataFrame, ntc_headroom: pd.Series | None = None) -> pd.DataFrame:
    """Per-hour features for one zone.

    `cap_by_tech`/`srmc_by_tech` are [t × tech] available capacity and SRMC (the latter from the
    **exogenous** price vector, so a finer fuel series sharpens these without touching the model);
    `prices` is [t × {gas,coal,oil,co2}].
    """
    firm = cap_by_tech.sum(axis=1)
    load = residual_load.reindex(idx)
    out = pd.DataFrame(index=idx)
    # --- structural ratios (the extrapolatable core) ---
    out["tightness"] = load / (firm + EPS)
    out["res_share"] = res_pot.reindex(idx) / (load + res_pot.reindex(idx) + EPS)
    out["log_firm_cap"] = np.log1p(firm)                      # scale context, not a level in €
    # NB: a "reserve margin" of (firm-load)/firm is exactly 1 - tightness, so it is deliberately absent —
    # it would add a perfectly collinear column, not information. Outage state enters through `firm`
    # itself (REMIT-derated), which `log_firm_cap` and `avail_derate` carry.
    out["avail_derate"] = firm / (firm.rolling(720, min_periods=24).max() + EPS)

    # --- merit order: where residual load sits on this zone's own supply curve ---
    techs = [c for c in cap_by_tech.columns if c in srmc_by_tech.columns]
    cum, ss = supply_curves(cap_by_tech[techs].to_numpy(float), srmc_by_tech[techs].to_numpy(float))
    out = out.join(pd.DataFrame(curve_knots(cum, ss), index=idx))
    out["srmc_at_residual"] = srmc_at(cum, ss, load.to_numpy(float))
    out["curve_slope"] = out["curve_q90"] - out["curve_q50"]   # how steep the peaking end is

    # --- fuel economics: levels AND the ordering signals that generalise ---
    p = prices.reindex(idx)
    for c in ("gas", "coal", "oil", "co2"):
        out[f"px_{c}"] = p[c]
    sp = pd.DataFrame([fuel_spreads(r) for r in p.to_dict("records")], index=idx)
    out = out.join(sp.add_prefix("sp_"))

    if ntc_headroom is not None:
        out["ntc_headroom"] = ntc_headroom.reindex(idx)

    # --- lags: the ramp/commitment memory the CRF transitions cannot see in the design matrix ---
    for lag in LAGS:
        out[f"d_tight_{lag}"] = out["tightness"].diff(lag)
        out[f"d_srmc_{lag}"] = out["srmc_at_residual"].diff(lag)
    out["d_load_1"] = load.diff(1)
    out["ramp_rate"] = load.diff(1) / (firm + EPS)             # normalised ramp demanded of the fleet

    return out.join(_calendar(idx))


def add_neighbour_context(feats: dict[str, pd.DataFrame], zones: list[str]) -> dict[str, pd.DataFrame]:
    """Cross-zone context: how tight the *neighbours* are. Market coupling means a zone's price is often
    set abroad (France imports its price in ~60 % of hours), so the surrounding system state is a genuine
    predictor — and unlike observed prices it is available in projection."""
    tight = pd.DataFrame({z: feats[z]["tightness"] for z in zones if z in feats})
    out = {}
    for z in feats:
        others = [o for o in tight.columns if o != z]
        f = feats[z].copy()
        if others:
            f["nb_tight_mean"] = tight[others].mean(axis=1)
            f["nb_tight_max"] = tight[others].max(axis=1)
            f["tight_rel_nb"] = f["tightness"] - f["nb_tight_mean"]
        out[z] = f
    return out


FORBIDDEN = ("price_observed", "srmc_implied", "gen_", "tranche", "setting_zone", "confidence")


def assert_no_leakage(feats: pd.DataFrame) -> None:
    """Guard the no-leakage rule: fail loudly if an outcome-derived column reaches the design matrix."""
    bad = [c for c in feats.columns if any(k in c for k in FORBIDDEN)]
    if bad:
        raise ValueError(f"outcome-derived columns must not be features: {bad}")
