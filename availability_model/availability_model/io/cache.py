"""Disk cache for expensive full-table DB scans (e.g. the p99.9 capacity), keyed by DB mtime.

Thin wrapper over `powersim_core.cache` (shared mechanism); only the config-specific key + cache dir live
here so the behaviour is byte-identical to before the core extraction.
"""
from __future__ import annotations

from collections.abc import Callable

import pandas as pd

from powersim_core.cache import disk_cached, mtime_key

from ..config import Config


def db_key(config: Config, extra: str = "") -> str:
    db = config.resolve(config.section("data")["sqlite_path"])
    return mtime_key(db, extra=extra)


def cached(config: Config, name: str, key: str, compute: Callable[[], pd.DataFrame]) -> pd.DataFrame:
    return disk_cached(config.path.parent / ".cache", name, key, compute)
