"""Driver: pull the ERA5 100 m wind + SSRD extract for every station + offshore farm.

Cached/resumable — safe to re-run; only missing points are fetched. Long-running (CDS queue).
    python scripts/pull_era5.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from res_model.config import load_config  # noqa: E402
from res_model.io.era5 import build_points, download_points  # noqa: E402


def main() -> None:
    cfg = load_config(str(Path(__file__).resolve().parents[1] / "config.yaml"))
    pts = build_points(cfg)
    print(f"[pull_era5] {len(pts)} points → {cfg.resolve(cfg.section('era5')['cache_dir'])}", flush=True)
    done = download_points(cfg, pts)
    n_ok = sum(1 for p in done.values() if p.exists() and p.stat().st_size > 0)
    print(f"[pull_era5] complete: {n_ok}/{len(pts)} point files present", flush=True)


if __name__ == "__main__":
    main()
