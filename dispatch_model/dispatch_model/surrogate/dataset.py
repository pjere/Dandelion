"""Assemble the surrogate training panel: features (projection-available) + labels (weak-supervised).

Splits are **by year, never by hour**. Hours inside a week are massively autocorrelated, so a random
hour split would leak neighbouring hours across the boundary and flatter the model badly. Train on
2019+2022+2023 (normal / gas-crisis / transition — the same multi-regime logic as the markup panel) and
hold out **2024** untouched.

**CH is excluded by default.** Switzerland is hydro-dominated and hydro's opportunity cost is an
endogenous water value (the LP's budget dual), not an SRMC — so an SRMC-derived label cannot represent it.
Measured: 15-23 % label agreement and a 37-104 €/MWh implied-vs-observed error, against 5-7 €/MWh for the
other zones in 2019. Including it would train the model on noise. Fixing it properly needs a water-value
head (see `tranches.py`), which is deliberately out of scope here.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .features import assert_no_leakage

LAYER, DATASET = "dispatch", "surrogate_panel"
#: hydro-dominated: SRMC-based labels cannot represent an endogenous water value (see module docstring)
EXCLUDE_ZONES = ("CH",)
TRAIN_YEARS = (2019, 2022, 2023)
HOLDOUT_YEARS = (2024,)
MIN_CONFIDENCE = 0.25          # below this the label is too ambiguous to learn from


def build_panel(features: dict[str, pd.DataFrame], labels: pd.DataFrame,
                exclude_zones=EXCLUDE_ZONES, min_confidence: float = MIN_CONFIDENCE) -> pd.DataFrame:
    """Join per-zone features to their labels → one long panel [timestamp_utc, zone, features…, target…].

    Rows keep their `confidence` so training can weight them; only genuinely unusable rows (no label, or
    confidence below `min_confidence`) are dropped, and the drop is reported by `panel_summary`.
    """
    keep = [z for z in features if z not in set(exclude_zones)]
    frames = []
    for z in keep:
        f = features[z].copy()
        assert_no_leakage(f)
        f.insert(0, "zone", z)
        f.insert(0, "timestamp_utc", f.index)
        lab = labels[labels["zone"] == z][
            ["timestamp_utc", "zone", "setting_zone", "tranche_tech", "regime", "confidence",
             "srmc_implied", "price_observed"]]
        frames.append(f.reset_index(drop=True).merge(lab, on=["timestamp_utc", "zone"], how="inner",
                                                     validate="one_to_one"))
    panel = pd.concat(frames, ignore_index=True)
    panel["year"] = pd.to_datetime(panel["timestamp_utc"]).dt.year
    panel["usable"] = panel["tranche_tech"].notna() & (panel["confidence"] >= min_confidence)
    return panel.sort_values(["zone", "timestamp_utc"]).reset_index(drop=True)


def split(panel: pd.DataFrame, train_years=TRAIN_YEARS, holdout_years=HOLDOUT_YEARS):
    """Temporal split — by year, never by hour (see module docstring).

    **Both sides keep every hour of their years, including unusable ones.** Filtering the training split
    to `usable` rows looks harmless and is not: it punches holes in the hourly grid, and since sequences
    must break at any gap, the 168 h chains then form only out of unusually long runs of confidently
    labelled hours. Measured when this was got wrong: FR training data fragmented into 1674 blocks of
    median length 9 h, so most of it was discarded outright and what survived was a biased easy subsample
    — while the CRF's whole partial-supervision mechanism sat unused. Unusable hours must instead reach
    the model as `-1` and be marginalised (`model.to_sequences`, `crf._loss_grad`).
    """
    tr = panel[panel["year"].isin(train_years)]
    ho = panel[panel["year"].isin(holdout_years)]
    return tr.reset_index(drop=True), ho.reset_index(drop=True)


def panel_summary(panel: pd.DataFrame) -> pd.DataFrame:
    """Per (zone, year): rows, usable share, mean confidence and class balance — the honesty report that
    must be read before trusting anything trained on this."""
    rows = []
    for (z, y), g in panel.groupby(["zone", "year"]):
        vc = g.loc[g["usable"], "tranche_tech"].value_counts(normalize=True)
        rows.append({"zone": z, "year": y, "rows": len(g),
                     "pct_usable": 100 * g["usable"].mean(),
                     "mean_conf": g["confidence"].mean(),
                     "top_class": vc.index[0] if len(vc) else None,
                     "top_class_pct": 100 * float(vc.iloc[0]) if len(vc) else np.nan,
                     "n_classes": int(g.loc[g["usable"], "tranche_tech"].nunique())})
    return pd.DataFrame(rows).sort_values(["zone", "year"])


def with_zone_dummies(panel: pd.DataFrame, zones: list[str] | None = None) -> pd.DataFrame:
    """One-hot the zone into the design matrix.

    One model is fitted across all zones so that shared physics (a merit order is a merit order) is learned
    once from five times the data. But the zones are *not* interchangeable — France is nuclear-based, Spain
    is gas-and-wind, Italy is gas-dominated — so without an identity the model would have to average over
    fleets it can distinguish. The dummies let it share structure and still specialise. They extrapolate
    fine: the zone set is fixed by construction, unlike a calendar-year dummy which could not reach 2046.
    """
    zs = sorted(zones or panel["zone"].unique())
    out = panel.copy()
    for z in zs:
        out[f"z_{z}"] = (out["zone"] == z).astype(float)
    return out


def feature_columns(panel: pd.DataFrame) -> list[str]:
    """The design-matrix columns — everything that is not an identifier or an outcome."""
    drop = {"timestamp_utc", "zone", "year", "usable", "setting_zone", "tranche_tech", "regime",
            "confidence", "srmc_implied", "price_observed", "abs_err_eur", "margin_eur",
            "agrees_price_match"}
    return [c for c in panel.columns if c not in drop]


def write_panel(panel: pd.DataFrame):
    from powersim_core import lake
    return lake.write_table(panel, LAYER, DATASET, index=False)


def read_panel() -> pd.DataFrame:
    from powersim_core import lake
    return lake.read_table(LAYER, DATASET)
