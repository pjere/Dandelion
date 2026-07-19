"""Generic mtime-keyed disk cache (§5) — the single home for the near-identical `io/cache.py` in
availability and dispatch. Caches a DataFrame to Parquet under `cache_dir`, keyed by a string that the
caller derives from source-file mtimes, so it auto-invalidates when inputs change.
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pandas as pd


def mtime_key(*paths, extra: str = "") -> str:
    """Cache key from the newest mtime among `paths` (+ optional extra discriminator)."""
    mt = max((os.path.getmtime(p) for p in paths if Path(p).exists()), default=0.0)
    return f"{mt:.0f}|{extra}"


def disk_cached(cache_dir, name: str, key: str, compute: Callable[[], pd.DataFrame]) -> pd.DataFrame:
    """Return cached `name` if its stored key matches, else compute + store."""
    d = Path(cache_dir)
    pq, meta = d / f"{name}.parquet", d / f"{name}.meta.json"
    if pq.exists() and meta.exists():
        try:
            if json.loads(meta.read_text()).get("key") == key:
                return pd.read_parquet(pq)
        except (ValueError, OSError):
            pass
    df = compute()
    d.mkdir(parents=True, exist_ok=True)
    df.to_parquet(pq, index=False)
    meta.write_text(json.dumps({"key": key}))
    return df
