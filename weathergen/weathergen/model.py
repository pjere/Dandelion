"""Serializable container for all fitted objects (fit-once / simulate-many)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from powersim_core.serialize import load_dataclass, save_dataclass


@dataclass
class FittedModel:
    """Everything ``simulate`` needs, serialized to ``models/`` after ``fit``.

    The sub-models (climatology, transforms, marginals, dependence, trend) are the
    phase objects; each is independently picklable.
    """

    config_raw: dict[str, Any]
    station_meta: pd.DataFrame          # id, lat, lon, elevation, lst_offset_h
    var_names: list[str]
    station_ids: list[str]
    climatology: Any = None             # Phase 2
    transforms: Any = None              # Phase 3
    marginals: Any = None               # Phase 4
    dependence: Any = None              # Phase 5
    trend: Any = None                   # Phase 6
    meta: dict[str, Any] = field(default_factory=dict)

    def save(self, path: str | Path) -> Path:
        """Portable JSON + npz sidecar via the recursive dataclass serializer (no pickle — REVIEW F6).
        The nested phase objects (climatology/transforms/marginals/dependence/trend) are all dataclasses
        of arrays/scalars, so the whole tree round-trips without executing arbitrary code on load."""
        self.meta.setdefault("saved_at", datetime.now(UTC).isoformat())
        return save_dataclass(self, Path(path).with_suffix(".json"))

    @staticmethod
    def load(path: str | Path) -> FittedModel:
        return load_dataclass(Path(path).with_suffix(".json"))
