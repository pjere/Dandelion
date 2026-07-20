"""Turn the panel into CRF-ready sequences, fit, and score — the training bridge.

**Sequences are weekly windows (168 h), matching the LP's own window granularity.** That is not
cosmetic: the LP couples a week through the hydro budget and the §51 trigger, so when the surrogate later
escalates to the exact LP it must escalate a *whole window*. Training on the same unit keeps the two paths
commensurable.

**Standardisation is fitted on the training years only** and reused unchanged for the holdout. Fitting it
on all data would leak 2024's distribution into training and quietly flatter the very gate that is
supposed to catch over-fitting.

Unlabelled and low-confidence hours become `-1` and are **marginalised** by the CRF rather than dropped
(see `crf.py`) — dropping them would sever the chains and bias training toward easy hours.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .crf import CRF

SEQ_LEN = 168          # one week, the LP's escalation unit


@dataclass
class Standardiser:
    """Median-impute then z-score, with statistics frozen from the training split."""
    median: np.ndarray = field(default_factory=lambda: np.zeros(0))
    mean: np.ndarray = field(default_factory=lambda: np.zeros(0))
    std: np.ndarray = field(default_factory=lambda: np.zeros(0))

    def fit(self, X: np.ndarray) -> Standardiser:
        self.median = np.nanmedian(X, axis=0)
        self.median = np.where(np.isfinite(self.median), self.median, 0.0)
        Z = np.where(np.isfinite(X), X, self.median)
        self.mean, self.std = Z.mean(axis=0), Z.std(axis=0)
        self.std = np.where(self.std > 1e-9, self.std, 1.0)
        return self

    def apply(self, X: np.ndarray) -> np.ndarray:
        Z = np.where(np.isfinite(X), X, self.median)
        return np.clip((Z - self.mean) / self.std, -8, 8)      # clip: 2046 states must not explode

    def to_dict(self) -> dict:
        return {"median": self.median, "mean": self.mean, "std": self.std}

    @classmethod
    def from_dict(cls, d) -> Standardiser:
        return cls(np.asarray(d["median"]), np.asarray(d["mean"]), np.asarray(d["std"]))


def class_vocab(panel: pd.DataFrame) -> list[str]:
    """Stable, sorted tranche vocabulary — index order must not depend on row order."""
    return sorted(panel.loc[panel["usable"], "tranche_tech"].dropna().unique())


def to_sequences(panel: pd.DataFrame, feature_cols: list[str], classes: list[str],
                 seq_len: int = SEQ_LEN):
    """Panel → `X` [B, L, F], `Y` [B, L] (-1 = unlabelled), and a per-sequence index frame.

    Sequences never straddle a zone or a time gap; incomplete trailing windows are dropped.
    """
    cls_idx = {c: i for i, c in enumerate(classes)}
    Xs, Ys, meta = [], [], []
    for zone, g in panel.sort_values(["zone", "timestamp_utc"]).groupby("zone", sort=True):
        g = g.reset_index(drop=True)
        ts = pd.to_datetime(g["timestamp_utc"])
        # break the chain wherever the hourly grid is interrupted
        brk = np.flatnonzero(ts.diff().dt.total_seconds().to_numpy()[1:] != 3600.0) + 1
        for blk in np.split(np.arange(len(g)), brk):
            n = (len(blk) // seq_len) * seq_len
            if n == 0:
                continue
            sub = g.iloc[blk[:n]]
            X = sub[feature_cols].to_numpy(float).reshape(-1, seq_len, len(feature_cols))
            y = sub["tranche_tech"].map(cls_idx).to_numpy(dtype=object)
            y = np.where(sub["usable"].to_numpy() & pd.notna(y), y, -1).astype(int)
            Xs.append(X)
            Ys.append(y.reshape(-1, seq_len))
            meta.append(pd.DataFrame({"zone": zone,
                                      "start": sub["timestamp_utc"].to_numpy()[::seq_len]}))
    if not Xs:
        raise ValueError("no complete sequences — check the panel's time grid")
    return np.concatenate(Xs), np.concatenate(Ys), pd.concat(meta, ignore_index=True)


@dataclass
class TranchePredictor:
    """The fitted surrogate: standardiser + class vocabulary + CRF."""
    classes: list[str]
    feature_cols: list[str]
    scaler: Standardiser
    crf: CRF

    def predict(self, X: np.ndarray, use_chain: bool = True) -> np.ndarray:
        Z = self._z(X)
        return self.crf.predict(Z) if use_chain else self.crf.predict_no_chain(Z)

    def marginals(self, X: np.ndarray) -> np.ndarray:
        return self.crf.marginals(self._z(X))

    def _z(self, X: np.ndarray) -> np.ndarray:
        B, L, F = X.shape
        return self.scaler.apply(X.reshape(-1, F)).reshape(B, L, F)

    def to_dict(self) -> dict:
        return {"classes": list(self.classes), "feature_cols": list(self.feature_cols),
                "scaler": self.scaler.to_dict(), "crf": self.crf.to_dict()}

    @classmethod
    def from_dict(cls, d: dict) -> TranchePredictor:
        return cls(list(d["classes"]), list(d["feature_cols"]),
                   Standardiser.from_dict(d["scaler"]), CRF.from_dict(d["crf"]))


def fit_predictor(train: pd.DataFrame, feature_cols: list[str], classes: list[str],
                  hidden: int | None = 24, l2: float = 1e-3, max_iter: int = 200,
                  seq_len: int = SEQ_LEN, seed: int = 0) -> tuple[TranchePredictor, np.ndarray, np.ndarray]:
    """Fit on the training split; returns the predictor plus the sequences it was fitted on."""
    X, Y, _ = to_sequences(train, feature_cols, classes, seq_len)
    B, L, F = X.shape
    scaler = Standardiser().fit(X.reshape(-1, F))
    Z = scaler.apply(X.reshape(-1, F)).reshape(B, L, F)
    crf = CRF(len(classes), hidden=hidden, l2=l2, max_iter=max_iter).fit(Z, Y, seed=seed)
    return TranchePredictor(classes, feature_cols, scaler, crf), X, Y


def accuracy_report(pred: np.ndarray, Y: np.ndarray, classes: list[str]) -> pd.DataFrame:
    """Per-class recall/support on labelled positions only (-1 positions have no ground truth)."""
    m = Y >= 0
    rows = [{"tranche": c,
             "support": int((Y[m] == i).sum()),
             "recall_pct": 100 * float((pred[m][Y[m] == i] == i).mean()) if (Y[m] == i).any() else np.nan}
            for i, c in enumerate(classes)]
    rows.append({"tranche": "ALL", "support": int(m.sum()),
                 "recall_pct": 100 * float((pred[m] == Y[m]).mean())})
    return pd.DataFrame(rows)
