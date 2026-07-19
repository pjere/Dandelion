"""Configuration loading. One YAML drives the whole pipeline (no magic numbers in code)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import yaml


@dataclass
class Config:
    """Thin typed wrapper around the parsed ``config.yaml`` mapping."""

    raw: dict[str, Any]
    path: Path

    # convenience accessors --------------------------------------------------
    @property
    def seed(self) -> int:
        return int(self.raw["run"]["seed"])

    @property
    def variables(self) -> dict[str, dict]:
        return self.raw["variables"]

    @property
    def var_names(self) -> list[str]:
        return list(self.raw["variables"].keys())

    def section(self, name: str) -> dict[str, Any]:
        return self.raw.get(name, {})

    def resolve(self, rel: str | None) -> Path | None:
        """Resolve a path from the config relative to the config file location."""
        if rel is None:
            return None
        p = Path(rel)
        return p if p.is_absolute() else (self.path.parent / p).resolve()

    @property
    def models_dir(self) -> Path:
        return (self.path.parent / self.raw["run"]["models_dir"]).resolve()

    @property
    def reports_dir(self) -> Path:
        return (self.path.parent / self.raw["run"]["reports_dir"]).resolve()

    def rng(self) -> np.random.Generator:
        """The single seeded generator threaded through the whole pipeline."""
        return np.random.default_rng(self.seed)


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return Config(raw=raw, path=path)
