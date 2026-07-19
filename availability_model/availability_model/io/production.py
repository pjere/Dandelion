"""Phase 1 — per-unit production loader (the outage-inference input).

There is no REMIT/outage table in the DB (D1), so outage history is read off `rte_generation_per_unit`.
Aggregation to daily means is done in SQL (`DATE(ts_utc)`): it collapses the 14.4M hourly rows to
~790k unit-days and hands back per-day hour coverage, which we use to reject data gaps (a missing
morning must not read as an outage).
"""
from __future__ import annotations

import sqlite3

import pandas as pd

from ..config import Config
from .cache import cached, db_key


def daily_unit_output(config: Config) -> pd.DataFrame:
    """Daily mean production per unit → columns [unit_id, day, mean_mw, hours].

    `hours` is the number of hourly samples that fell in the day (≤24); it is the coverage flag used
    downstream to drop partially-missing days. `day` is a tz-naive calendar date (UTC). Result is disk-
    cached (invalidated by DB mtime) — the underlying scan reads the whole per-unit table.
    """
    d = config.section("data")
    per = d["per_unit_table"]
    period = d.get("period", {})

    def _compute() -> pd.DataFrame:
        con = sqlite3.connect(config.resolve(d["sqlite_path"]))
        try:
            df = pd.read_sql(
                f'SELECT series_key AS unit_id, DATE(ts_utc) AS day, '
                f'       AVG(value) AS mean_mw, COUNT(*) AS hours '
                f'FROM "{per}" WHERE value IS NOT NULL '
                f'GROUP BY series_key, DATE(ts_utc)', con)
        finally:
            con.close()
        df["day"] = pd.to_datetime(df["day"])
        if period.get("start"):
            df = df[df["day"] >= pd.Timestamp(period["start"])]
        if period.get("end"):
            df = df[df["day"] <= pd.Timestamp(period["end"])]
        return df.sort_values(["unit_id", "day"]).reset_index(drop=True)

    key = db_key(config, extra=f'{period.get("start")}:{period.get("end")}')
    return cached(config, "daily_unit_output", key, _compute)
