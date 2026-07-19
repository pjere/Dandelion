"""Phase 2 — hydro reservoir energy-budget calibration from RTE water reserves.

The availability model owns the reservoir *energy budget* constraint (how much stored energy is
available and its seasonal shape); the weather-driven hydro *production* itself is owned by res_model
(iv) to avoid double-counting. From the weekly hydraulic stock series we take the usable energy
capacity and the seasonal fill climatology (drawn down over winter, refilled by spring snowmelt).
Run-of-river availability follows the res_model inflow chain, so only a pass-through hook is kept here.
"""
from __future__ import annotations

import sqlite3

import pandas as pd

from ..config import Config


def calibrate_inflows(config: Config) -> dict:
    d = config.section("data")
    tbl = d["water_reserves_table"]
    con = sqlite3.connect(config.resolve(d["sqlite_path"]))
    try:
        wr = pd.read_sql(f'SELECT ts_utc, value FROM "{tbl}" WHERE value IS NOT NULL ORDER BY ts_utc', con)
    finally:
        con.close()
    wr["ts"] = pd.to_datetime(wr["ts_utc"])
    stock_gwh = wr["value"] / 1000.0                                # MWh -> GWh
    cap = float(stock_gwh.max())
    floor = float(stock_gwh.min())
    # seasonal climatology: mean stock by ISO week, normalised to mean 1
    wk = wr["ts"].dt.isocalendar().week.astype(int).clip(1, 52)
    clim = stock_gwh.groupby(wk).mean()
    clim = (clim / clim.mean()).round(3)
    return {
        "reservoir": {
            "energy_capacity_gwh": round(cap, 0),
            "usable_energy_gwh": round(cap - floor, 0),
            "min_stock_gwh": round(floor, 0),
            "seasonal_profile_week": {int(k): float(v) for k, v in clim.items()},
            "n_weeks": int(len(wr)), "source": "rte_water_reserves",
        },
        "ror": {"note": "run-of-river availability follows res_model (iv) inflow chain", "source": "res_model"},
        "pumped": {"cycle_efficiency": 0.75, "source": "literature"},
    }
