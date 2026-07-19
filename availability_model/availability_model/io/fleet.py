"""Phase 0 — build the fleet registry from the DB units + a domain lookup for nuclear palier/cooling.

The DB (`dim_production_unit`) has EIC + name + fuel_type but no palier / cooling / commissioning, so
those are supplied here from a maintained lookup of the French nuclear sites (the CP0/CPY/P4/P'4/N4/EPR
standardised series — the *paliers* that drive common-mode risk) and joined by plant name. Capacities
come from observed max production. The result pre-fills the workbook `fleet_registry` sheet; the user
owns closures / lifetime extensions / new builds there.
"""
from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

from ..config import Config
from .cache import cached, db_key

# site -> (palier, cooling, basin, site_commissioning_year, closure_year). cooling: sea/estuary → no
# summer thermal derating; river → weather-linked derating (§6.4). Keys must match the DB label prefixes
# (RTE abbreviates "ST ALBAN"/"ST LAURENT", not "SAINT-…"). Maintained domain defaults (verify).
_NUC_SITE = {
    "BUGEY": ("CP0", "river", "Rhône", 1978, None), "TRICASTIN": ("CPY", "river", "Rhône", 1980, None),
    "GRAVELINES": ("CPY", "sea", "NorthSea", 1980, None), "DAMPIERRE": ("CPY", "river", "Loire", 1980, None),
    "BLAYAIS": ("CPY", "estuary", "Gironde", 1981, None), "CHINON": ("CPY", "river", "Loire", 1984, None),
    "CRUAS": ("CPY", "river", "Rhône", 1984, None), "ST LAURENT": ("CPY", "river", "Loire", 1983, None),
    "PALUEL": ("P4", "sea", "Channel", 1984, None), "FLAMANVILLE": ("P4", "sea", "Channel", 1986, None),
    "ST ALBAN": ("P4", "river", "Rhône", 1985, None), "CATTENOM": ("P'4", "river", "Moselle", 1986, None),
    "BELLEVILLE": ("P'4", "river", "Loire", 1987, None), "NOGENT": ("P'4", "river", "Seine", 1987, None),
    "PENLY": ("P'4", "sea", "Channel", 1990, None), "GOLFECH": ("P'4", "river", "Garonne", 1990, None),
    "CHOOZ": ("N4", "river", "Meuse", 1996, None), "CIVAUX": ("N4", "river", "Vienne", 1997, None),
    "FESSENHEIM": ("CP0", "river", "Rhin", 1977, 2020),   # oldest CP0, shut down 2020 (excl. projection)
}
_FUEL2TECH = {
    "NUCLEAR": "nuclear", "FOSSIL_GAS": "gas", "FOSSIL_HARD_COAL": "coal", "FOSSIL_OIL": "oil",
    "BIOMASS": "biomass", "HYDRO_WATER_RESERVOIR": "hydro_reservoir",
    "HYDRO_PUMPED_STORAGE": "hydro_pumped", "HYDRO_RUN_OF_RIVER_AND_POUNDAGE": "hydro_ror",
}


def _nuc_meta(name: str) -> tuple:
    up = name.upper()
    if "FLAMANVILLE 3" in up:                          # the EPR, not the P4 units 1-2
        return ("EPR", "sea", "Channel", 2024, None)
    for site, meta in _NUC_SITE.items():
        if up.startswith(site):
            return meta
    return (None, "unknown", None, None, None)


def build_fleet_registry(config: Config, scenario: str = "reference") -> pd.DataFrame:
    d = config.section("data")
    con = sqlite3.connect(config.resolve(d["sqlite_path"]))
    try:
        reg = pd.read_sql(f'SELECT eic_code, canonical_name, fuel_type FROM {d["registry_table"]}', con)
    finally:
        con.close()

    # capacity = p99.9 of observed production per unit. NOT MAX(value): single-sample data spikes
    # inflate MAX wildly (e.g. CATTENOM 1 spikes to 32 GW vs its 1300 MW rating), whereas a high
    # quantile lands on the true rated Pmax because baseload units run flat at rating for long stretches
    # (verified: CATTENOM 1 p99.9=1308, BELLEVILLE 1 p99.9=1312 MW). Disk-cached (whole-table scan).
    def _cap() -> pd.DataFrame:
        con2 = sqlite3.connect(config.resolve(d["sqlite_path"]))
        try:
            return pd.read_sql(
                f'SELECT eic, value AS cap FROM ('
                f'  SELECT series_key AS eic, value,'
                f'         ROW_NUMBER() OVER (PARTITION BY series_key ORDER BY value) AS rn,'
                f'         COUNT(*)     OVER (PARTITION BY series_key)                AS n'
                f'  FROM "{d["per_unit_table"]}" WHERE value IS NOT NULL'
                f') WHERE rn = MAX(1, CAST(0.999 * n AS INTEGER))', con2)
        finally:
            con2.close()

    cap = cached(config, "unit_capacity_p999", db_key(config), _cap)
    reg = reg[reg["fuel_type"].isin(_FUEL2TECH)].copy()
    capm = dict(zip(cap["eic"], cap["cap"]))
    rows = []
    for _, r in reg.iterrows():
        tech = _FUEL2TECH[r["fuel_type"]]
        c = float(capm.get(r["eic_code"], np.nan))
        if not np.isfinite(c) or c <= 0:
            continue
        palier, cooling, basin, comm, closure = (
            _nuc_meta(r["canonical_name"]) if tech == "nuclear"
            else (None, "tower" if tech in ("coal", "gas") else "none", None, None, None))
        rows.append({"unit_id": r["eic_code"], "name": r["canonical_name"], "technology": tech,
                     "palier": palier, "capacity_mw": round(c, 0), "cooling": cooling, "basin": basin,
                     "commissioning_year": comm, "closure_year": closure, "scenario": scenario})
    return pd.DataFrame(rows).sort_values(["technology", "name"]).reset_index(drop=True)
