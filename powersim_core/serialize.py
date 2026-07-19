"""Safe model-parameter serialization (§6, resolves F6 — no pickle).

Pickle is non-portable (breaks across library versions) and unsafe (arbitrary code on load). Fitted models
here are just parameters — nested dicts/lists/scalars + numpy arrays — so we serialize them as **JSON**
(the structure) with an **npz sidecar** (the arrays), reconstructed losslessly. Portable, inspectable,
safe to load from anywhere.

    save_params(asdict(model), "model.json")     # writes model.json (+ model.json.npz if arrays present)
    payload = load_params("model.json")           # exact round-trip

Model classes provide `to_dict`/`from_dict` (or use `dataclasses.asdict`) — string keys only (convert
tuple keys like (month, hour) to "m,h" in the model layer; JSON has no tuple keys).
"""
from __future__ import annotations

import importlib
import json
from dataclasses import fields, is_dataclass
from pathlib import Path

import numpy as np

_ARRAY_TAG = "__ndarray__"
_DC_TAG = "__dataclass__"
_FRAME_TAG = "__dataframe__"
_SERIES_TAG = "__series__"


def _split_arrays(obj, arrays: dict):
    if isinstance(obj, np.ndarray):
        key = f"a{len(arrays)}"
        arrays[key] = obj
        return {_ARRAY_TAG: key}
    if isinstance(obj, dict):
        return {k: _split_arrays(v, arrays) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_split_arrays(v, arrays) for v in obj]
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    if isinstance(obj, float) and (np.isnan(obj) or np.isinf(obj)):
        return None
    return obj


def _join_arrays(obj, npz):
    if isinstance(obj, dict):
        if _ARRAY_TAG in obj:
            return np.asarray(npz[obj[_ARRAY_TAG]])
        return {k: _join_arrays(v, npz) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_join_arrays(v, npz) for v in obj]
    return obj


def save_params(payload: dict, path: str | Path) -> Path:
    """Write `payload` (nested dicts/lists/scalars/arrays) to `path` (.json) + `path`.npz for arrays."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    arrays: dict = {}
    tree = _split_arrays(payload, arrays)
    path.write_text(json.dumps(tree, indent=1), encoding="utf-8")
    npz = path.with_suffix(path.suffix + ".npz")
    if arrays:
        np.savez(npz, **arrays)
    elif npz.exists():
        npz.unlink()                                       # stale sidecar from a prior array-bearing save
    return path


def load_params(path: str | Path) -> dict:
    """Reconstruct the payload written by `save_params`."""
    path = Path(path)
    tree = json.loads(path.read_text(encoding="utf-8"))
    npz_path = path.with_suffix(path.suffix + ".npz")
    npz = np.load(npz_path) if npz_path.exists() else {}
    return _join_arrays(tree, npz)


# ---------------------------------------------------------------------------
# Recursive (nested-dataclass) serialization — for composite fitted models like
# weathergen's FittedModel whose fields are themselves dataclasses of arrays.
# Every node is tagged with its fully-qualified type and rebuilt via the class
# constructor with keyword fields (no arbitrary-code execution, unlike pickle).
# Leaves: ndarray (→ npz), np scalars (→ python), pandas Series/DataFrame, plain
# scalars/str/bool/None, and dict/list containers. Dict keys must be strings.
# ---------------------------------------------------------------------------
def _to_tree(obj, arrays: dict):
    import pandas as pd
    if is_dataclass(obj) and not isinstance(obj, type):
        node = {_DC_TAG: f"{type(obj).__module__}:{type(obj).__qualname__}"}
        for f in fields(obj):
            node[f.name] = _to_tree(getattr(obj, f.name), arrays)
        return node
    if isinstance(obj, np.ndarray):
        if obj.dtype == object:                        # object arrays can't go to npz safely → list
            return [_to_tree(v, arrays) for v in obj.tolist()]
        key = f"a{len(arrays)}"
        arrays[key] = obj
        return {_ARRAY_TAG: key}
    if isinstance(obj, pd.Series):
        return {_SERIES_TAG: {"index": list(obj.index), "values": _to_tree(obj.to_numpy(), arrays),
                              "name": obj.name}}
    if isinstance(obj, pd.DataFrame):
        return {_FRAME_TAG: {"index": list(obj.index),
                             "cols": {c: _to_tree(obj[c].to_numpy(), arrays) for c in obj.columns}}}
    if isinstance(obj, dict):
        return {k: _to_tree(v, arrays) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_tree(v, arrays) for v in obj]
    if isinstance(obj, (np.floating, np.integer)):
        return obj.item()
    if isinstance(obj, float) and (np.isnan(obj) or np.isinf(obj)):
        return None
    return obj


def _from_tree(node, npz):
    import pandas as pd
    if isinstance(node, dict):
        if _ARRAY_TAG in node:
            return np.asarray(npz[node[_ARRAY_TAG]])
        if _SERIES_TAG in node:
            s = node[_SERIES_TAG]
            return pd.Series(_from_tree(s["values"], npz), index=s["index"], name=s["name"])
        if _FRAME_TAG in node:
            fr = node[_FRAME_TAG]
            data = {c: _from_tree(v, npz) for c, v in fr["cols"].items()}
            return pd.DataFrame(data, index=fr["index"])
        if _DC_TAG in node:
            mod, qn = node[_DC_TAG].split(":")
            cls = getattr(importlib.import_module(mod), qn)
            kw = {k: _from_tree(v, npz) for k, v in node.items() if k != _DC_TAG}
            return cls(**kw)
        return {k: _from_tree(v, npz) for k, v in node.items()}
    if isinstance(node, list):
        return [_from_tree(v, npz) for v in node]
    return node


def save_dataclass(obj, path: str | Path) -> Path:
    """Serialize a (possibly nested) dataclass instance to portable JSON + npz sidecar."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    arrays: dict = {}
    tree = _to_tree(obj, arrays)
    path.write_text(json.dumps(tree), encoding="utf-8")
    npz = path.with_suffix(path.suffix + ".npz")
    if arrays:
        np.savez(npz, **arrays)
    elif npz.exists():
        npz.unlink()
    return path


def load_dataclass(path: str | Path):
    """Reconstruct the dataclass instance written by `save_dataclass`."""
    path = Path(path)
    tree = json.loads(path.read_text(encoding="utf-8"))
    npz_path = path.with_suffix(path.suffix + ".npz")
    npz = np.load(npz_path, allow_pickle=False) if npz_path.exists() else {}
    return _from_tree(tree, npz)
