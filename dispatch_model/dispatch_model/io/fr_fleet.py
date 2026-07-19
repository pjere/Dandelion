"""FR dispatchable fleet reader (DB) — unit id / technology / capacity for the unit-level stack.

Self-contained (no cross-package import of availability_model): capacity = p99.9 of per-unit production
(robust to data spikes, same rationale as step v). Availability over time is injected at solve time
(historical actuals for backtest, step-v draws for projection).
"""
from __future__ import annotations

import sqlite3

import pandas as pd

from ..config import Config

_FUEL2TECH = {
    "NUCLEAR": "nuclear", "FOSSIL_GAS": "gas", "FOSSIL_HARD_COAL": "coal", "FOSSIL_OIL": "oil",
    "BIOMASS": "biomass", "HYDRO_WATER_RESERVOIR": "hydro_reservoir",
    "HYDRO_PUMPED_STORAGE": "hydro_psp", "HYDRO_RUN_OF_RIVER_AND_POUNDAGE": "hydro_ror",
}


def load_fr_fleet(config: Config) -> pd.DataFrame:
    """→ [unit_id, name, tech, capacity_mw] for FR dispatchable units. Capacity scan is disk-cached."""
    from .cache import cached, db_key
    d = config.section("data")
    con = sqlite3.connect(config.resolve(d["sqlite_path"]))
    try:
        reg = pd.read_sql("SELECT eic_code, canonical_name, fuel_type FROM dim_production_unit", con)
    finally:
        con.close()

    def _cap() -> pd.DataFrame:
        con2 = sqlite3.connect(config.resolve(d["sqlite_path"]))
        try:
            return pd.read_sql(
                "SELECT eic AS unit_id, value AS cap FROM ("
                "  SELECT series_key AS eic, value,"
                "         ROW_NUMBER() OVER (PARTITION BY series_key ORDER BY value) AS rn,"
                "         COUNT(*)     OVER (PARTITION BY series_key)                AS n"
                "  FROM rte_generation_per_unit WHERE value IS NOT NULL"
                ") WHERE rn = MAX(1, CAST(0.999 * n AS INTEGER))", con2)
        finally:
            con2.close()

    cap = cached(config, "fr_unit_capacity_p999", db_key(config), _cap)
    reg = reg[reg["fuel_type"].isin(_FUEL2TECH)].copy()
    capm = dict(zip(cap["unit_id"], cap["cap"]))
    reg["tech"] = reg["fuel_type"].map(_FUEL2TECH)
    reg["capacity_mw"] = reg["eic_code"].map(capm)
    reg = reg[(reg["capacity_mw"].fillna(0) > 0)]
    return (reg.rename(columns={"eic_code": "unit_id", "canonical_name": "name"})
            [["unit_id", "name", "tech", "capacity_mw"]].reset_index(drop=True))
