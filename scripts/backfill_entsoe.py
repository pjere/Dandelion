"""Backfill ENTSO-E prices / load / generation / flows for the 7 dispatch zones.

Backtest + neighbour-calibration years: 2019 (normal), 2022 (crisis), 2023-24 (high RES). Idempotent
(ingest_log skips finished yearly chunks), so it is safe to re-run / resume after an interruption.

Usage:  python scripts/backfill_entsoe.py            # default years
        python scripts/backfill_entsoe.py 2018 2020  # explicit years
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # project root on path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from pricemodeling.db import get_engine  # noqa: E402
from pricemodeling.entsoe import series as entsoe_series  # noqa: E402

DEFAULT_YEARS = [2019, 2022, 2023, 2024]
DB_URL = "sqlite:///data/pricemodeling.db"


def main(years):
    import os
    eng = get_engine(DB_URL)
    cl = entsoe_series._client(os.getenv("ENTSOE_TOKEN"))
    for y in years:
        s, e = date(y, 1, 1), date(y, 12, 31)
        print(f"=== {y} ===", flush=True)
        print(f"  prices : {entsoe_series.ingest_prices(eng, cl, s, e)}", flush=True)
        print(f"  load   : {entsoe_series.ingest_load(eng, cl, s, e)}", flush=True)
        print(f"  gen    : {entsoe_series.ingest_generation(eng, cl, s, e)}", flush=True)
        print(f"  flows  : {entsoe_series.ingest_flows(eng, cl, s, e)}", flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    yrs = [int(a) for a in sys.argv[1:]] or DEFAULT_YEARS
    main(yrs)
