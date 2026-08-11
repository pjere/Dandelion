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


def _read_long(config, table, year, extra_cols=("series_key",), zones=None):
    """Long-schema slice for `year`, restricted to `zones` IN SQL rather than in pandas afterwards.

    The zone restriction used to live in the callers, applied to the frame once it was already in memory:
    every request for one zone-year read — and materialised — the whole year for EVERY zone. Measured on
    `entsoe_generation` (23.8 M rows) asking for DE_LU 2024:

        read whole year, filter in pandas   4 013 876 rows   8.90 s
        series_key pushed into SQL            562 166 rows   2.44 s   -> x3.6, identical output

    The cost is the read itself (7.73 s of the 8.90), not the timestamp parse (0.72 s) — 3.5 M rows are
    transported into pandas only to be discarded. `_read_long` is shared by prices, load and generation,
    so every zone-scoped read in the model paid it.

    Empty `zones` deliberately adds no clause: `IN ()` is not valid SQL, and the callers' own
    `isin` then yields the empty frame exactly as before.
    """
    d = config.section("data")["entsoe"]
    tbl = d[table]
    zl = [str(z) for z in zones] if zones is not None else []
    con = _conn(config)
    try:
        sql = (f'SELECT ts_utc, series_key, sub_key, value FROM "{tbl}" '
               f"WHERE value IS NOT NULL{_year_clause(year)}")
        if zl:
            sql += " AND series_key IN (" + ",".join("?" * len(zl)) + ")"
        df = pd.read_sql(sql, con, params=tuple(zl))
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
    df = _read_long(config, "prices_table", year, zones=zones)
    if zones is not None:
        df = df[df["series_key"].isin(zones)]
    out = _to_hourly(df, ["series_key"]).rename(columns={"series_key": "zone", "value": "price_eur_mwh"})
    return out.dropna(subset=["price_eur_mwh"]).reset_index(drop=True)


def load_demand_hist(config: Config, year: int | None = None, zones=None) -> pd.DataFrame:
    df = _read_long(config, "load_table", year, zones=zones)
    if zones is not None:
        df = df[df["series_key"].isin(zones)]
    out = _to_hourly(df, ["series_key"]).rename(columns={"series_key": "zone", "value": "load_mw"})
    return out.dropna(subset=["load_mw"]).reset_index(drop=True)


def load_generation_hist(config: Config, year: int | None = None, zones=None) -> pd.DataFrame:
    df = _read_long(config, "generation_table", year, zones=zones)
    if zones is not None:
        df = df[df["series_key"].isin(zones)]
    df["tech"] = df["sub_key"].map(PSR2TECH).fillna(df["sub_key"])
    out = _to_hourly(df, ["series_key", "tech"]).rename(
        columns={"series_key": "zone", "value": "gen_mw"})
    return out.dropna(subset=["gen_mw"]).reset_index(drop=True)


def load_installed_capacity(config: Config, zone: str, year: int) -> dict[str, float]:
    """→ {tech: installed_MW} for a zone/year from entsoe_installed_capacity (nameplate).

    Falls back to the NEAREST available year when the requested year is missing: the split-cluster zones
    (NL/AT/CZ/PL/DK/SI) were ingested for 2019 only, and a stale nameplate still beats the caller's
    p99.9-of-generation proxy — measured on NL 2024: proxy 9.1 GW gas vs 15.6 GW real fleet, i.e. half the
    CCGT fleet invisible and the zone artificially scarce (+22 €/MWh level bias, zero negative prints)."""
    con = _conn(config)
    try:
        df = pd.read_sql("SELECT ts_utc, sub_key, value FROM entsoe_installed_capacity "
                         f"WHERE series_key = '{zone}'", con)
    except Exception:  # noqa: BLE001  (table may not exist yet)
        return {}
    finally:
        con.close()
    if df.empty:
        return {}
    yrs = pd.to_datetime(df["ts_utc"]).dt.year
    nearest = int(min(yrs.unique(), key=lambda y: abs(int(y) - int(year))))
    df = df[yrs == nearest]
    df["tech"] = df["sub_key"].map(PSR2TECH).fillna(df["sub_key"])
    return df.groupby("tech")["value"].sum().to_dict()


def load_flows_hist(config: Config, year: int | None = None) -> pd.DataFrame:
    df = _read_long(config, "flows_table", year)
    out = _to_hourly(df, ["series_key"]).rename(columns={"series_key": "border", "value": "flow_mw"})
    return out.dropna(subset=["flow_mw"]).reset_index(drop=True)
