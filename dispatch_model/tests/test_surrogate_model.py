"""Panel → sequence bridge: chains must never straddle a zone or a time gap, and the standardiser must
be fitted on training data alone. Both failures are silent and would corrupt every downstream number."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from dispatch_model.surrogate import dataset as ds
from dispatch_model.surrogate.model import (
    Standardiser,
    TranchePredictor,
    accuracy_report,
    class_vocab,
    fit_predictor,
    to_sequences,
)

FEATS = ["tightness", "srmc_at_residual"]


def test_train_split_keeps_unusable_hours_so_chains_stay_whole():
    """Regression: filtering the train split to `usable` punches holes in the hourly grid, sequences then
    break at every hole, and the CRF's partial-supervision path goes unused. Measured cost when this was
    wrong: FR training fragmented to a median 9 h block, most data discarded."""
    idx = pd.date_range("2019-01-01", periods=400, freq="h", tz="UTC")
    panel = pd.DataFrame({
        "timestamp_utc": idx, "zone": "FR", "year": 2019, "usable": True,
        "tranche_tech": "gas", "confidence": 1.0,
        "tightness": 0.5, "srmc_at_residual": 40.0})
    panel.loc[100:104, "usable"] = False            # a few ambiguous hours in the middle
    tr, _ = ds.split(panel)
    assert len(tr) == 400, "unusable hours must survive the split, not be dropped"
    X, Y, _ = to_sequences(tr, FEATS, ["gas"], seq_len=168)
    assert X.shape[0] == 2, "the chain must stay contiguous across the ambiguous hours"
    assert (Y[0, 100:105] == -1).all()


def _panel(zones=("FR", "BE"), hours=336, start="2019-01-01", gap_at=None):
    rows = []
    for z in zones:
        idx = pd.date_range(start, periods=hours, freq="h", tz="UTC")
        if gap_at is not None:
            idx = idx.delete(gap_at)
        rng = np.random.default_rng(abs(hash(z)) % 1000)
        rows.append(pd.DataFrame({
            "timestamp_utc": idx, "zone": z, "year": idx.year,
            "tightness": rng.normal(size=len(idx)),
            "srmc_at_residual": rng.normal(size=len(idx)),
            "tranche_tech": "gas", "usable": True, "confidence": 1.0}))
    return pd.concat(rows, ignore_index=True)


def test_sequences_never_straddle_zones():
    p = _panel(hours=336)
    X, Y, meta = to_sequences(p, FEATS, ["gas"], seq_len=168)
    assert X.shape == (4, 168, 2)                     # 2 zones x 2 complete weeks
    assert set(meta["zone"]) == {"FR", "BE"}
    assert (meta["zone"].value_counts() == 2).all()


def test_sequences_break_at_time_gaps_and_drop_partial_windows():
    # 336 h with one hour removed => a 200 h block and a 135 h block => only one complete 168 h window
    p = _panel(zones=("FR",), hours=336, gap_at=200)
    X, _, _ = to_sequences(p, FEATS, ["gas"], seq_len=168)
    assert X.shape[0] == 1
    assert X.shape[1] == 168


def test_sequence_hours_are_contiguous():
    p = _panel(zones=("FR",), hours=168)
    _, _, meta = to_sequences(p, FEATS, ["gas"], seq_len=168)
    assert len(meta) == 1


def test_unlabelled_positions_become_minus_one_not_dropped():
    p = _panel(zones=("FR",), hours=168)
    p.loc[5:9, "usable"] = False
    X, Y, _ = to_sequences(p, FEATS, ["gas"], seq_len=168)
    assert X.shape[0] == 1                            # the chain survives intact
    assert (Y[0, 5:10] == -1).all()
    assert (Y[0, 10:] == 0).all()


def test_class_vocab_is_sorted_and_ignores_unusable():
    p = _panel(zones=("FR",), hours=48)
    p.loc[:10, "tranche_tech"] = "coal"
    p.loc[11:20, ["tranche_tech", "usable"]] = ["lignite", False]
    assert class_vocab(p) == ["coal", "gas"]          # lignite was never usable


def test_standardiser_imputes_nan_and_clips_outliers():
    X = np.array([[1.0, 10.0], [3.0, 20.0], [np.nan, 30.0]])
    s = Standardiser().fit(X)
    out = s.apply(np.array([[np.nan, 1e9]]))
    assert np.isfinite(out).all()
    assert abs(out[0, 1]) <= 8.0                      # clipped, so a 2046 state cannot explode
    assert np.isclose(out[0, 0], (s.median[0] - s.mean[0]) / s.std[0])


def test_standardiser_stats_come_from_train_only():
    train = np.random.default_rng(0).normal(0, 1, (500, 2))
    s = Standardiser().fit(train)
    before = (s.mean.copy(), s.std.copy())
    s.apply(np.random.default_rng(1).normal(50, 10, (500, 2)))     # scoring must not refit
    assert np.array_equal(s.mean, before[0]) and np.array_equal(s.std, before[1])


def test_fit_predictor_roundtrips_and_reports():
    p = _panel(zones=("FR", "BE"), hours=336)
    rng = np.random.default_rng(0)
    p["tranche_tech"] = np.where(rng.random(len(p)) < 0.5, "gas", "coal")
    classes = class_vocab(p)
    pred, X, Y = fit_predictor(p, FEATS, classes, hidden=None, max_iter=15)
    out = pred.predict(X)
    assert out.shape == Y.shape
    assert pred.marginals(X).shape == (*Y.shape, len(classes))
    rep = accuracy_report(out, Y, classes)
    assert set(rep["tranche"]) == {*classes, "ALL"}
    assert rep.loc[rep["tranche"] == "ALL", "support"].iloc[0] == (Y >= 0).sum()
    back = TranchePredictor.from_dict(pred.to_dict())
    assert np.array_equal(back.predict(X), out)


def test_to_sequences_raises_when_nothing_complete():
    p = _panel(zones=("FR",), hours=50)
    with pytest.raises(ValueError, match="no complete sequences"):
        to_sequences(p, FEATS, ["gas"], seq_len=168)
