"""Configuration loading (single YAML drives the pipeline; no hard-coded paths/constants).

Same pattern as step (iii): a thin typed wrapper — the run block is pydantic-validated, the rest is
accessed by section. Paths resolve relative to the config file.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import yaml
from pydantic import BaseModel


class RunCfg(BaseModel):
    seed: int = 20260708
    models_dir: str = "models"
    reports_dir: str = "reports"
    output_dir: str = "output"


class Config:
    def __init__(self, raw: dict[str, Any], path: Path):
        self.raw = raw
        self.path = path
        self.run = RunCfg(**raw.get("run", {}))

    def section(self, name: str) -> dict[str, Any]:
        return self.raw.get(name, {})

    def resolve(self, rel: str | None) -> Path | None:
        if rel is None:
            return None
        p = Path(rel)
        return p if p.is_absolute() else (self.path.parent / p).resolve()

    @property
    def models_dir(self) -> Path:
        return (self.path.parent / self.run.models_dir).resolve()

    @property
    def reports_dir(self) -> Path:
        return (self.path.parent / self.run.reports_dir).resolve()

    @property
    def output_dir(self) -> Path:
        return (self.path.parent / self.run.output_dir).resolve()

    @property
    def seed(self) -> int:
        return self.run.seed

    def rng(self) -> np.random.Generator:
        return np.random.default_rng(self.run.seed)


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return Config(raw=raw, path=path)
