"""Reproducibility metadata (§5) — one implementation, replacing the ≥3 duplicate `meta.py`.

Stamps every output with the hashes that make a run reproducible from (inputs, config, seed): git hash,
config hash, scenario/workbook snapshot hash, draw_id, seed, plus the powersim_core version. Mandatory on
every write.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any


def git_hash(cwd: Path | None = None) -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=cwd,
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "nogit"
    except (OSError, subprocess.SubprocessError):
        return "nogit"


def file_hash(path: str | Path | None, cap_bytes: int | None = None) -> str:
    if path is None or not Path(path).exists():
        return "absent"
    h = hashlib.sha256()
    n = 0
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
            if cap_bytes and n >= cap_bytes:
                break
    return h.hexdigest()[:16]


def run_metadata(*, config_path: str | Path | None = None, workbook_path: str | Path | None = None,
                 weather_cube_path: str | Path | None = None, scenario_id: str | None = None,
                 draw_id: int | None = None, seed: int | None = None,
                 git_cwd: str | Path | None = None, **extra: Any) -> dict[str, Any]:
    from . import __version__
    md = {
        "git_hash": git_hash(Path(git_cwd) if git_cwd else None),
        "config_hash": file_hash(config_path),
        "workbook_hash": file_hash(workbook_path),
        "weather_cube_hash": file_hash(weather_cube_path, cap_bytes=64 << 20),
        "scenario_id": scenario_id,
        "draw_id": None if draw_id is None else int(draw_id),
        "seed": None if seed is None else int(seed),
        "powersim_core_version": __version__,
    }
    md.update(extra)
    return md
