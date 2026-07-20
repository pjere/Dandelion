"""Map a predicted marginal tranche to a price, and score it — the gate that decides the surrogate.

The mapping is **analytic**, which is what lets a classifier trained on 2019-24 produce sensible 2046
levels: the model picks *which tranche* is marginal, and the tranche's SRMC is then evaluated at that
hour's exogenous fuel/CO2 prices (`tranches.tranche_srmc`). So the price level extrapolates through the
commodity inputs even though the classifier itself only ever interpolates. A finer fuel series sharpens
every projected price without retraining anything.

Two regimes escape the merit order entirely and must not be papered over:

- **negative prices** are set by RES support floors and the §51 trigger, not by any thermal SRMC;
- **scarcity** prices sit far above the dearest running unit's SRMC.

Neither is recoverable from a tranche label, so both are flagged for the deferral detector rather than
predicted here. Reporting one blended error number across all three regimes would hide exactly the hours
the LP is needed for, so `price_metrics` scores them separately.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def map_to_price(pred_idx: np.ndarray, classes: list[str], srmc_by_tech: pd.DataFrame) -> np.ndarray:
    """SRMC of the predicted tranche, per hour. `srmc_by_tech` is [t x tech] at that hour's fuel prices."""
    cols = list(srmc_by_tech.columns)
    lut = {i: cols.index(c) for i, c in enumerate(classes) if c in cols}
    arr = srmc_by_tech.to_numpy(float)
    out = np.full(len(arr), np.nan)
    flat = np.asarray(pred_idx).ravel()[:len(arr)]
    for i, j in lut.items():
        m = flat == i
        if m.any():
            out[m] = arr[m, j]
    return out


def price_metrics(pred_price: np.ndarray, observed: np.ndarray, regime: np.ndarray | None = None,
                  label: str = "") -> pd.DataFrame:
    """MAE / RMSE / bias / correlation, reported **per regime** — a blended number hides the hard hours."""
    rows = []

    def _row(name, p, o):
        m = np.isfinite(p) & np.isfinite(o)
        if m.sum() < 2:
            return {"model": label, "regime": name, "n": int(m.sum()), "mae": np.nan,
                    "rmse": np.nan, "bias": np.nan, "corr": np.nan}
        d = p[m] - o[m]
        return {"model": label, "regime": name, "n": int(m.sum()),
                "mae": float(np.abs(d).mean()), "rmse": float(np.sqrt((d ** 2).mean())),
                "bias": float(d.mean()), "corr": float(np.corrcoef(p[m], o[m])[0, 1])}

    rows.append(_row("all", pred_price, observed))
    if regime is not None:
        for r in ("thermal", "negative", "scarcity"):
            m = np.asarray(regime) == r
            if m.any():
                rows.append(_row(r, np.asarray(pred_price)[m], np.asarray(observed)[m]))
    return pd.DataFrame(rows)


def compare(models: dict[str, np.ndarray], observed: np.ndarray,
            regime: np.ndarray | None = None) -> pd.DataFrame:
    """Score several price series on identical hours — the only fair way to rank them."""
    return pd.concat([price_metrics(p, observed, regime, label=k) for k, p in models.items()],
                     ignore_index=True)
