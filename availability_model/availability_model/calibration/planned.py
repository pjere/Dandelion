"""Phase 2 — planned-outage calibration (nuclear ASR / VP / VD) from the inferred catalogue.

Fits, per palier:
  - duration distribution per outage type (lognormal on log-days) — routine outages only, so we drop
    crisis events and anything outside the type's config day-band (that discards extraordinary tails
    like Paluel 2's 992-day generator drop, which belong to the forced/tail side, not routine VD).
  - refuelling cycle length (median gap between successive planned-outage starts on a unit).
  - seasonal placement weights (month histogram of planned-outage starts) — EDF concentrates refuelling
    Apr–Sep, and the scheduler must reproduce that or it flattens winter scarcity.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

_TYPES = ("ASR", "VP", "VD")


def _lognorm(days: np.ndarray) -> dict:
    d = days[days > 0]
    if d.size < 3:
        return {"mean_days": float(np.mean(d)) if d.size else np.nan, "lognorm_mu": np.nan,
                "lognorm_sigma": np.nan, "n": int(d.size)}
    lg = np.log(d)
    return {"mean_days": float(d.mean()), "lognorm_mu": float(lg.mean()),
            "lognorm_sigma": float(lg.std(ddof=1)), "n": int(d.size)}


# default cycle length (months) per palier when the unit's own history is too thin to fit (e.g. the
# brand-new EPR at Flamanville 3, whose ~1 yr of startup data has no meaningful refuelling cycle).
_DEFAULT_CYCLE = {"CP0": 12.0, "CPY": 12.0, "P4": 18.0, "P'4": 18.0, "N4": 16.0, "EPR": 18.0}
_MIN_PLANNED = 15                                                  # below this, a palier is "unreliable"


def _fit_group(planned: pd.DataFrame, bands: dict) -> dict:
    types = {}
    for t in _TYPES:
        band = bands[t]
        d = planned.loc[planned["outage_type"] == t, "duration_days"].to_numpy()
        d = d[(d >= band["min"]) & (d <= band["max"])]              # routine only (drop tails)
        types[t] = _lognorm(d)
    gaps = []
    for _, u in planned.sort_values("start").groupby("unit_id"):
        if len(u) >= 2:
            gaps.extend(u["start"].diff().dropna().dt.days.tolist())
    cycle_months = float(np.median(gaps) / 30.44) if gaps else np.nan
    m = planned["start"].dt.month.value_counts().reindex(range(1, 13), fill_value=0)
    w = (m / m.mean()).round(3) if m.sum() else pd.Series(1.0, index=range(1, 13))
    return {"cycle_months": cycle_months, "types": types,
            "seasonality": {int(k): float(v) for k, v in w.items()}, "n_planned": int(len(planned))}


def calibrate_planned(events: pd.DataFrame, registry: pd.DataFrame, config) -> dict:
    bands = config.section("calibration")["planned_types"]          # {ASR:{min,max}, VP:..., VD:...}
    pal = registry.set_index("unit_id")["palier"]
    ev = events[(events["technology"] == "nuclear") & (~events["in_crisis"])].copy()
    ev["palier"] = ev["unit_id"].map(pal)
    planned_all = ev[ev["outage_type"].isin(_TYPES)]
    pooled = _fit_group(planned_all, bands)                         # fleet-wide fallback for thin paliers

    out: dict[str, dict] = {}
    for palier, g in ev.groupby("palier"):
        fit = _fit_group(g[g["outage_type"].isin(_TYPES)], bands)
        reliable = fit["n_planned"] >= _MIN_PLANNED and 10 <= (fit["cycle_months"] or 0) <= 26
        if not reliable:                                            # borrow pooled shape; keep palier cycle
            fit = {"cycle_months": _DEFAULT_CYCLE.get(str(palier), 18.0),
                   "types": {t: (fit["types"][t] if fit["types"][t]["n"] >= 5 else pooled["types"][t])
                             for t in _TYPES},
                   "seasonality": pooled["seasonality"], "n_planned": fit["n_planned"]}
        fit["reliable"] = bool(reliable)
        out[str(palier)] = fit
    return out
