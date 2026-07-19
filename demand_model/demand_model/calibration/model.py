"""Phase 3 — the calibrated additive model (separable components, serialisable)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from powersim_core.serialize import load_params, save_params

from .design import make_design


@dataclass
class CalibratedModel:
    intercept: float
    coef: pd.Series                     # index = feature name
    groups: dict[str, list[str]]
    tau_heat: float
    tau_cool: float
    tau_cold: float
    halflives_h: list[int]
    metrics: dict[str, Any] = field(default_factory=dict)

    def _design(self, feat: pd.DataFrame) -> pd.DataFrame:
        X, _ = make_design(feat, self.tau_heat, self.tau_cool, self.tau_cold)
        return X.reindex(columns=self.coef.index, fill_value=0.0)

    def predict(self, feat: pd.DataFrame) -> pd.Series:
        X = self._design(feat)
        return pd.Series(X.to_numpy() @ self.coef.to_numpy() + self.intercept, index=feat.index, name="load_mw")

    def component(self, feat: pd.DataFrame, group: str, include_intercept: bool = False) -> pd.Series:
        """Evaluate one separable component (base/heat/cool/light/anomaly) — used by projection."""
        X = self._design(feat)
        gcols = [c for c in self.groups[group] if c in X.columns]
        val = X[gcols].to_numpy() @ self.coef.reindex(gcols).to_numpy()
        if include_intercept:
            val = val + self.intercept
        return pd.Series(val, index=feat.index, name=group)

    def components(self, feat: pd.DataFrame) -> pd.DataFrame:
        out = {g: self.component(feat, g) for g in self.groups}
        out["base"] = out["base"] + self.intercept          # attach intercept to base
        return pd.DataFrame(out, index=feat.index)

    def save(self, path: str | Path) -> Path:
        """Portable JSON (no pickle — REVIEW F6); the `coef` Series is stored as index + values array."""
        payload = asdict(self)
        payload["coef"] = {"index": list(self.coef.index), "values": self.coef.to_numpy(),
                           "name": self.coef.name}
        return save_params(payload, Path(path).with_suffix(".json"))

    @staticmethod
    def load(path: str | Path) -> CalibratedModel:
        d = load_params(Path(path).with_suffix(".json"))
        c = d["coef"]
        d["coef"] = pd.Series(c["values"], index=c["index"], name=c["name"])
        return CalibratedModel(**d)
