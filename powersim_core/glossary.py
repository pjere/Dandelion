"""Domain glossary (§4) — canonical term → used identically in code, DataFrame/DB columns, config keys,
file names. Units are in the name for every physical quantity; never a bare `value`/`data`/`temp`.

This module is the single source of truth for column names so the per-package renames (F1/F2/F3) all
target the same strings.
"""
from __future__ import annotations

# --- identifiers ---
TIMESTAMP_UTC = "timestamp_utc"     # tz-aware UTC, always (replaces ts_utc / time)
SCENARIO_ID = "scenario_id"
DRAW_ID = "draw_id"
YEAR = "year"
ZONE = "zone"
REGION = "region"
UNIT_ID = "unit_id"
TECHNOLOGY = "technology"
BORDER = "border"

# --- physical quantities (unit suffix mandatory) ---
LOAD_MW = "load_mw"
AVAILABLE_MW = "available_mw"
PRODUCTION_MW = "production_mw"
CAPACITY_MW = "capacity_mw"
CF = "cf"                           # dimensionless capacity factor
TEMPERATURE_C = "temperature_c"
WIND_SPEED_MS = "wind_speed_ms"
GHI_WM2 = "ghi_wm2"
PRICE_EUR_MWH = "price_eur_mwh"
INFLOW_MWH = "inflow_mwh"

# legacy → canonical timestamp aliases the loaders currently emit (used by rename shims during migration)
TIMESTAMP_ALIASES = ("ts_utc", "time", "timestamp", "datetime")

# whitelisted abbreviations (the only ones allowed unexpanded)
GLOSSARY_ABBREVIATIONS = {
    "cf": "capacity factor", "mw": "megawatt", "mwh": "megawatt-hour", "ghi": "global horizontal irradiance",
    "utc": "coordinated universal time", "ntc": "net transfer capacity", "srmc": "short-run marginal cost",
    "psp": "pumped-storage", "ror": "run-of-river", "res": "renewable energy sources", "eic": "energy id code",
}

# verb semantics (§4) — the same verb means the same thing in all models
VERB_SEMANTICS = {
    "load": "read from storage", "build": "construct in memory", "fit": "estimate parameters from data",
    "calibrate": "fit + adjust to targets", "project": "generate a forward scenario",
    "generate": "draw stochastically", "validate": "check against acceptance criteria", "write": "persist",
}


def canonical_timestamp(df, col: str | None = None):
    """Rename whichever legacy timestamp column exists to TIMESTAMP_UTC (migration shim)."""
    if col and col in df.columns:
        return df.rename(columns={col: TIMESTAMP_UTC})
    for a in TIMESTAMP_ALIASES:
        if a in df.columns and TIMESTAMP_UTC not in df.columns:
            return df.rename(columns={a: TIMESTAMP_UTC})
    return df
