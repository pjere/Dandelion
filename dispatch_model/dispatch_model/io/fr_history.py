"""FR historical demand + must-take RES + actual generation from `master_hourly` (RTE, complete 2014-2026).

Used to build the FR net load for backtests without any ENTSO-E dependency. Must-take RES = solar + wind
(on/off) + run-of-river (ROR is weather-driven must-run); reservoir hydro is dispatchable (water value,
Phase 6). Actual per-tech generation is returned too, for the historical availability proxy.
"""
from __future__ import annotations

import sqlite3

import pandas as pd

from ..config import Config

_MUSTTAKE = ["prod_solar", "prod_wind_onshore", "prod_wind_offshore",
             "prod_hydro_run_of_river_and_poundage"]
_GEN = {"prod_nuclear": "nuclear", "prod_fossil_gas": "gas", "prod_fossil_hard_coal": "coal",
        "prod_fossil_oil": "oil", "prod_biomass": "biomass", "prod_waste": "waste",
        "prod_hydro_water_reservoir": "hydro_reservoir", "prod_hydro_pumped_storage": "hydro_psp"}


def load_fr_netload(config: Config, start: str, end: str) -> pd.DataFrame:
    """→ hourly [timestamp_utc, demand_mw, musttake_res_mw, + actual gen_<tech>_mw] over [start, end).

    Reads the DB raw `ts_utc` column and emits the canonical `timestamp_utc` (glossary/ADR-3)."""
    cols = ["ts_utc", "conso_realised", *(_MUSTTAKE), *(_GEN)]
    con = sqlite3.connect(config.resolve(config.section("data")["sqlite_path"]))
    try:
        df = pd.read_sql(
            f'SELECT {", ".join(cols)} FROM master_hourly '
            f"WHERE ts_utc >= '{start}' AND ts_utc < '{end}' ORDER BY ts_utc", con)
    finally:
        con.close()
    df["timestamp_utc"] = pd.to_datetime(df["ts_utc"], utc=True)
    df["demand_mw"] = pd.to_numeric(df["conso_realised"], errors="coerce")
    df["musttake_res_mw"] = df[_MUSTTAKE].apply(pd.to_numeric, errors="coerce").clip(lower=0).sum(axis=1)
    out = df[["timestamp_utc", "demand_mw", "musttake_res_mw"]].copy()
    for col, tech in _GEN.items():
        out[f"gen_{tech}_mw"] = pd.to_numeric(df[col], errors="coerce").clip(lower=0)
    return out.dropna(subset=["demand_mw"]).reset_index(drop=True)
