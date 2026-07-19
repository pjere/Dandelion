"""Configuration loading (single YAML). Zone-agnostic: zones/borders come from config. Same pattern as
steps (iii)–(v), plus zone/border helpers and a degraded single-zone mode."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import yaml
from pydantic import BaseModel


class RunCfg(BaseModel):
    seed: int = 20260714
    mode: str = "multi_zone"          # multi_zone | single_zone
    models_dir: str = "models"
    reports_dir: str = "reports"
    output_dir: str = "output"
    resolution: str = "1h"


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

    # ---- zones / borders -----------------------------------------------------
    @property
    def zones(self) -> list[str]:
        """Active zones. In single_zone mode only the unit-resolved zone (FR) is modelled explicitly."""
        z = list(self.section("zones").keys())
        if self.run.mode == "single_zone":
            return [self.unit_resolved_zone]
        return z

    @property
    def all_zones(self) -> list[str]:
        return list(self.section("zones").keys())

    @property
    def unit_resolved_zone(self) -> str:
        for z, meta in self.section("zones").items():
            if meta.get("unit_resolved"):
                return z
        return next(iter(self.section("zones")))

    @property
    def borders(self) -> list[tuple[str, str]]:
        """Borders among active zones (empty in single_zone mode — borders become supply curves)."""
        if self.run.mode == "single_zone":
            return []
        zs = set(self.zones)
        return [(a, b) for a, b in self.section("borders") if a in zs and b in zs]

    def entsoe_code(self, zone: str) -> str:
        return self.section("zones")[zone].get("entsoe", zone)

    # ---- dirs / rng ----------------------------------------------------------
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

    def rng(self, draw: int = 0) -> np.random.Generator:
        from powersim_core.rng import draw_rng
        return draw_rng(self.run.seed, draw)                 # F4: single RNG authority (SeedSequence)


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return Config(raw=raw, path=path)
