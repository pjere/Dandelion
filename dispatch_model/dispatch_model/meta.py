"""Reproducibility metadata (git/config/workbook hashes) stamped on every output."""
from __future__ import annotations

from pathlib import Path  # noqa: F401 (used by run_metadata type hints below)
from typing import Any

from powersim_core.meta import file_hash as _file_hash  # noqa: F401  (shared hashing primitives, §5)
from powersim_core.meta import git_hash as _git_hash

from .config import Config


def run_metadata(config: Config, draw: int | str | None = None, seed: int | None = None) -> dict[str, Any]:
    wb = config.resolve(config.section("assumptions").get("workbook"))
    return {
        "git_hash": _git_hash(config.path.parent),
        "config_hash": _file_hash(config.path),
        "workbook_hash": _file_hash(wb),
        "weather_cube_hash": _file_hash(config.resolve(config.section("data").get("weathergen_output"))),
        "mode": config.run.mode,
        "zones": config.zones,
        "draw": str(draw) if draw is not None else "0",
        "seed": int(seed if seed is not None else config.seed),
        "dispatch_model_version": __import__("dispatch_model").__version__,
    }
