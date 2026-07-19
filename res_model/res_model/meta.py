"""Phase 0 — reproducibility metadata (§5.5, §7): stamp every output with the hashes that make a run
bit-reproducible from (inputs, config, seed)."""
from __future__ import annotations

from pathlib import Path  # noqa: F401 (used by run_metadata type hints below)
from typing import Any

from powersim_core.meta import file_hash as _file_hash  # noqa: F401  (shared hashing primitives, §5)
from powersim_core.meta import git_hash as _git_hash

from .config import Config


def run_metadata(config: Config, weather_draw: str | int | None = None,
                 seed: int | None = None) -> dict[str, Any]:
    """Provenance stamp attached to Parquet/report outputs."""
    wb = config.resolve(config.section("assumptions")["workbook"])
    return {
        "git_hash": _git_hash(config.path.parent),
        "config_hash": _file_hash(config.path),
        "workbook_hash": _file_hash(wb),
        "weather_cube_hash": _file_hash(config.resolve(config.section("weather")["weathergen_output"])),
        "weather_draw": str(weather_draw) if weather_draw is not None else "0",
        "seed": int(seed if seed is not None else config.seed),
        "res_model_version": __import__("res_model").__version__,
    }
