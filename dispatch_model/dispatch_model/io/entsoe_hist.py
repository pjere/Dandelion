"""Readers for the ingested ENTSO-E history (prices / load / generation / flows) → tidy hourly frames.

The DB stores native resolution (some zones 15-min post-2025); the dispatch LP runs hourly, so series
are resampled to hourly (mean). Used for backtesting and neighbour calibration. PSR names are mapped to
the model's technology classes.
"""
from __future__ import annotations

import sqlite3

import pandas as pd

from ..config import Config

# ENTSO-E PSR label -> model technology class
PSR2TECH = {
    "Nuclear": "nuclear", "Fossil Gas": "gas", "Fossil Hard coal": "coal",
    "Fossil Brown coal/Lignite": "lignite", "Fossil Oil": "oil", "Biomass": "biomass",
    "Waste": "waste", "Solar": "solar", "Wind Onshore": "wind_onshore",
    "Wind Offshore": "wind_offshore", "Hydro Run-of-river and poundage": "hydro_ror",
    "Hydro Water Reservoir": "hydro_reservoir", "Hydro Pumped Storage": "hydro_psp",
    "Geothermal": "geothermal", "Other": "other", "Other renewable": "other_res",
}


def _conn(config: Config) -> sqlite3.Connection:
    return sqlite3.connect(config.resolve(config.section("data")["sqlite_path"]))


def _year_clause(year: int | None) -> str:
    return f" AND ts_utc >= '{year}-01-01' AND ts_utc < '{year + 1}-01-01'" if year else ""


def _read_long(config, table, year, extra_cols=("series_key",)):
    d = config.section("data")["entsoe"]
    tbl = d[table]
    con = _conn(config)
    try:
        df = pd.read_sql(f'SELECT ts_utc, series_key, sub_key, value FROM "{tbl}" '
                         f"WHERE value IS NOT NULL{_year_clause(year)}", con)
    finally:
        con.close()
    df["ts_utc"] = pd.to_datetime(df["ts_utc"], utc=True)
    return df


def _to_hourly(df, group_cols, value="value"):
    """Resample each group's series to hourly mean (handles native 15-min zones).

    Emits the canonical `timestamp_utc` column (the DB raw column is `ts_utc`, renamed at this boundary
    per the glossary/ADR-3 — model-layer frames use `timestamp_utc` throughout)."""
    out = (df.set_index("ts_utc").groupby(group_cols)[value]
           .resample("1h").mean().reset_index()
           .rename(columns={"ts_utc": "timestamp_utc"}))
    return out


def load_prices(config: Config, year: int | None = None, zones=None) -> pd.DataFrame:
    df = _read_long(config, "prices_table", year)
    if zones is not None:
        df = df[df["series_key"].isin(zones)]
    out = _to_hourly(df, ["series_key"]).rename(columns={"series_key": "zone", "value": "price_eur_mwh"})
    return out.dropna(subset=["price_eur_mwh"]).reset_index(drop=True)


def load_demand_hist(config: Config, year: int | None = None, zones=None) -> pd.DataFrame:
    df = _read_long(config, "load_table", year)
    if zones is not None:
        df = df[df["series_key"].isin(zones)]
    out = _to_hourly(df, ["series_key"]).rename(columns={"series_key": "zone", "value": "load_mw"})
    return out.dropna(subset=["load_mw"]).reset_index(drop=True)


def load_generation_hist(config: Config, year: int | None = None, zones=None) -> pd.DataFrame:
    df = _read_long(config, "generation_table", year)
    if zones is not None:
        df = df[df["series_key"].isin(zones)]
    df["tech"] = df["sub_key"].map(PSR2TECH).fillna(df["sub_key"])
    out = _to_hourly(df, ["series_key", "tech"]).rename(
        columns={"series_key": "zone", "value": "gen_mw"})
    return out.dropna(subset=["gen_mw"]).reset_index(drop=True)


def load_installed_capacity(config: Config, zone: str, year: int) -> dict[str, float]:
    """→ {tech: installed_MW} for a zone/year from entsoe_installed_capacity (nameplate)."""
    con = _conn(config)
    try:
        df = pd.read_sql("SELECT sub_key, value FROM entsoe_installed_capacity "
                         f"WHERE series_key = '{zone}' AND ts_utc >= '{year}-01-01' "
                         f"AND ts_utc < '{year + 1}-01-01'", con)
    except Exception:  # noqa: BLE001  (table may not exist yet)
        return {}
    finally:
        con.close()
    if df.empty:
        return {}
    df["tech"] = df["sub_key"].map(PSR2TECH).fillna(df["sub_key"])
    return df.groupby("tech")["value"].sum().to_dict()


def load_flows_hist(config: Config, year: int | None = None) -> pd.DataFrame:
    df = _read_long(config, "flows_table", year)
    out = _to_hourly(df, ["series_key"]).rename(columns={"series_key": "border", "value": "flow_mw"})
    return out.dropna(subset=["flow_mw"]).reset_index(drop=True)
