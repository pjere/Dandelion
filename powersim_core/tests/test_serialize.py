"""Tests for the safe (no-pickle) serializer — round-trip of nested params + arrays."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from powersim_core.serialize import (
    load_dataclass,
    load_params,
    save_dataclass,
    save_params,
)


def test_roundtrip_scalars_and_nested(tmp_path):
    payload = {"a": 1, "b": 2.5, "s": "x", "lst": [1, 2, 3],
               "nested": {"c": {"d": 0.1}, "flags": [True, False]}}
    save_params(payload, tmp_path / "m.json")
    assert load_params(tmp_path / "m.json") == payload
    assert not (tmp_path / "m.json.npz").exists()          # no arrays → no sidecar


def test_roundtrip_with_arrays(tmp_path):
    payload = {"phi": np.array([0.1, 0.2, 0.3]),
               "chol": np.eye(3) * 2.0,
               "meta": {"n": 5, "cov": np.arange(4).reshape(2, 2).astype(float)}}
    save_params(payload, tmp_path / "m.json")
    assert (tmp_path / "m.json.npz").exists()
    back = load_params(tmp_path / "m.json")
    assert np.array_equal(back["phi"], payload["phi"])
    assert np.array_equal(back["chol"], payload["chol"])
    assert np.array_equal(back["meta"]["cov"], payload["meta"]["cov"])
    assert back["meta"]["n"] == 5


def test_numpy_scalars_and_nan_normalized(tmp_path):
    payload = {"x": np.float64(1.5), "n": np.int64(7), "bad": float("nan")}
    save_params(payload, tmp_path / "m.json")
    back = load_params(tmp_path / "m.json")
    assert back["x"] == 1.5 and back["n"] == 7 and back["bad"] is None


# --- nested-dataclass serializer (save_dataclass) ---
@dataclass
class _Leaf:
    coef: np.ndarray
    r2: float


@dataclass
class _Composite:
    name: str
    leaf: _Leaf
    leaves: list
    table: pd.DataFrame
    series: pd.Series
    meta: dict = field(default_factory=dict)


def test_save_dataclass_nested_roundtrip(tmp_path):
    obj = _Composite(
        name="fit",
        leaf=_Leaf(coef=np.array([1.0, 2.0, 3.0]), r2=0.97),
        leaves=[_Leaf(coef=np.arange(4.0), r2=0.5), _Leaf(coef=np.ones(2), r2=0.1)],
        table=pd.DataFrame({"id": ["a", "b"], "lat": [48.1, 43.6]}),
        series=pd.Series([0.1, 0.2], index=["p", "q"], name="w"),
        meta={"n": 5, "ssp": "245"},
    )
    save_dataclass(obj, tmp_path / "c.json")
    back = load_dataclass(tmp_path / "c.json")
    assert isinstance(back, _Composite) and isinstance(back.leaf, _Leaf)
    assert np.array_equal(back.leaf.coef, obj.leaf.coef) and back.leaf.r2 == 0.97
    assert np.array_equal(back.leaves[1].coef, np.ones(2))
    assert back.table.equals(obj.table)
    assert back.series.equals(obj.series)
    assert back.meta == {"n": 5, "ssp": "245"}
