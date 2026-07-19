"""Weekly reservoir energy budgets (guide curves) from historical FR reservoir generation + stock.

Two-level hydro decomposition, option (b): rather than an SRMC, reservoir hydro is bid at ~0 but limited
to a weekly energy budget derived from the historical seasonal generation profile (`master_hourly`
prod_hydro_water_reservoir), scaled by the year's wetness. Fed to the LP as a per-window energy cap; the
LP self-allocates the budget into the highest-price hours (peak-shaving) and the **water value** falls
out as the dual of that cap. The seasonal stock trajectory (`rte_water_reserves`) is exposed as the guide
curve for later refinement / an SDDP swap (option a).
"""
from __future__ import annotations

import sqlite3

import pandas as pd

from ..config import Config


def reservoir_generation_climatology(config: Config) -> pd.Series:
    """Mean reservoir generation energy per ISO week (MWh/week), averaged over history."""
    con = sqlite3.connect(config.resolve(config.section("data")["sqlite_path"]))
    try:
        df = pd.read_sql("SELECT ts_utc, prod_hydro_water_reservoir AS g FROM master_hourly "
                         "WHERE prod_hydro_water_reservoir IS NOT NULL", con)
    finally:
        con.close()
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    df["g"] = pd.to_numeric(df["g"], errors="coerce").clip(lower=0)          # MW hourly ≈ MWh/h
    df["isoweek"] = df["ts"].dt.isocalendar().week.astype(int).clip(1, 52)
    df["year"] = df["ts"].dt.year
    weekly = df.groupby(["year", "isoweek"])["g"].sum()                       # MWh per (year, week)
    return weekly.groupby("isoweek").mean().reindex(range(1, 53)).interpolate()


def weekly_reservoir_budget(config: Config, week_start: pd.Timestamp, wetness: float = 1.0) -> float:
    """Energy budget (MWh) available to reservoir generation for the week containing `week_start`."""
    clim = reservoir_generation_climatology(config)
    iso = min(int(pd.Timestamp(week_start).isocalendar().week), 52)
    return float(clim.get(iso, clim.mean()) * max(0.0, wetness))


def annual_wetness(config: Config, year: int) -> float:
    """Wetness factor = the year's reservoir generation / the historical mean (proxy for inflow)."""
    con = sqlite3.connect(config.resolve(config.section("data")["sqlite_path"]))
    try:
        df = pd.read_sql("SELECT ts_utc, prod_hydro_water_reservoir AS g FROM master_hourly "
                         "WHERE prod_hydro_water_reservoir IS NOT NULL", con)
    finally:
        con.close()
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    df["g"] = pd.to_numeric(df["g"], errors="coerce").clip(lower=0)
    annual = df.groupby(df["ts"].dt.year)["g"].sum()
    mean = annual[annual.index < 2026].mean()
    return float(annual.get(year, mean) / mean) if mean else 1.0
