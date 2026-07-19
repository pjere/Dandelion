"""Ingest the cached ERA5 point extract (era5_cache/*.zip) into the pricemodeling DB table
``era5_point_hourly`` so the wind conversions read 100 m wind from the DB (like SYNOP), not the zips.
Idempotent. Run once after pull_era5.py:
    python scripts/ingest_era5_db.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from res_model.config import load_config  # noqa: E402
from res_model.io.era5 import ingest_to_db  # noqa: E402


def main() -> None:
    cfg = load_config(str(Path(__file__).resolve().parents[1] / "config.yaml"))
    n = ingest_to_db(cfg)
    print(f"[era5-ingest] complete: {n} rows in era5_point_hourly", flush=True)


if __name__ == "__main__":
    main()
