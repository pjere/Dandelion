"""Full-year rolling backtest: solve the multi-zone LP over weekly windows and score vs observed prices.

Year data is preloaded once (FR net load + per-zone neighbour net loads + block stacks + reservoir
generation + observed prices), so the ~52 weekly windows are fast LP solves. Each window uses that
month's commodity prices for SRMC and the window's actual reservoir energy as the hydro budget. Scores
the §8 price metrics per zone (baseload error, quantile errors, correlation, negative/spike frequency,
FR–DE spread) — the acceptance gate. Generation/flow physics metrics are a documented extension.
"""
from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

from powersim_core import lake

from ..commodities.model import CommodityModel, load_zone_basis, zone_prices
from ..config import Config
from ..io.entsoe_hist import load_generation_hist
from ..io.fr_history import load_fr_netload
from ..neighbours.blocks import build_neighbour_stack, constituents, neighbour_netload
from ..res_schemes import load_res_schemes, solve_with_triggers
from ..rules import rules_at
from .assemble import _EXCLUDE_DISPATCH, NTC, _month_prices, flow_derived_ntc
from .windows import fr_stack_base, fr_window, nb_window


def _observed_prices(config, year, zones):
    con = sqlite3.connect(config.resolve(config.section("data")["sqlite_path"]))
    try:
        df = pd.read_sql("SELECT ts_utc, series_key, value FROM entsoe_day_ahead_prices "
                         "WHERE ts_utc >= ? AND ts_utc < ?",
                         con, params=(f"{year}-01-01", f"{year + 1}-01-01"))
    finally:
        con.close()
    df["timestamp_utc"] = pd.to_datetime(df["ts_utc"], utc=True)   # DB raw ts_utc → canonical
    out = {}
    for z in zones:
        s = df[df["series_key"] == z].set_index("timestamp_utc")["value"]
        out[z] = s[~s.index.duplicated()].resample("1h").mean() if not s.empty else None
    return out


def run_backtest(config: Config, year: int, n_weeks: int | None = None,
                 use_remit_nuclear_avail: bool = False, de_unit_level: bool = False) -> dict:
    zones = [z for z in config.all_zones if z != "GB"]
    neigh = [z for z in zones if z != "FR"]
    wb = config.resolve(config.section("assumptions")["workbook"])
    cm = CommodityModel.from_workbook(wb)
    basis = load_zone_basis(wb)                                 # per-zone gas hub (PSV/MIBGAS vs TTF)
    res_schemes = load_res_schemes(wb)                          # RES subsidy bid tranches per zone (§51)

    # ---- preload the year ----
    fr = load_fr_netload(config, f"{year}-01-01", f"{year + 1}-01-01").set_index("timestamp_utc")
    fr_stack = fr_stack_base(config)
    nb_stack = {}
    for z in list(neigh):
        try:                                    # DE_REST (virtual NL+AT+DK+PL+CZ) only has generation for
            if de_unit_level and z == "DE_LU":  # #73: unit-level DE thermal from the MaStR registry
                from ..neighbours.blocks import build_de_unit_stack
                nb_stack[z] = build_de_unit_stack(config, z, year)
            else:
                nb_stack[z] = build_neighbour_stack(config, z, year)   # 2019; drop it (and its borders) in years
        except (KeyError, ValueError):          # it lacks data rather than failing the whole backtest.
            neigh.remove(z)
            zones.remove(z)
    nb_stack = {z: s[~s["tech"].isin(_EXCLUDE_DISPATCH)].reset_index(drop=True) for z, s in nb_stack.items()}
    nb_nl = {z: neighbour_netload(config, z, year).set_index("timestamp_utc") for z in neigh}
    for z in list(neigh):                       # a zone with load data missing for this year → drop it (else
        if nb_nl[z].empty:                      # its empty net-load yields a degenerate LP time coord)
            neigh.remove(z); zones.remove(z); nb_stack.pop(z, None); nb_nl.pop(z, None)
    nb_res = {}
    for z in neigh:
        g = load_generation_hist(config, year, zones=constituents(z))   # virtual zones sum constituents
        res_g = g[g["tech"] == "hydro_reservoir"]
        nb_res[z] = (res_g.groupby("timestamp_utc")["gen_mw"].sum()
                     if not res_g.empty else pd.Series(dtype=float))
    obs = _observed_prices(config, year, zones)
    ntc = flow_derived_ntc(config, year)                        # effective NTC from realized flows
    nuc_unavail = None
    if use_remit_nuclear_avail:                                 # #78: true FR nuclear availability from REMIT
        from pricemodeling.config import load_settings
        from pricemodeling.db import get_engine
        from pricemodeling.entsoe.unavailability import nuclear_unavailable_mw
        nu = nuclear_unavailable_mw(get_engine(load_settings().db_url), "FR", year)
        nuc_unavail = nu if not nu.empty else None

    # ---- weekly windows ----
    weeks = pd.date_range(f"{year}-01-01", f"{year + 1}-01-01", freq="7D", tz="UTC")
    if n_weeks:
        weeks = weeks[:n_weeks + 1]
    price_chunks = []
    for w0, w1 in zip(weeks[:-1], weeks[1:]):
        T = fr.loc[(fr.index >= w0) & (fr.index < w1)].index
        if len(T) < 24:
            continue
        prices = _month_prices(cm, w0)
        zd = {"FR": fr_window(fr, fr_stack, zone_prices(prices, "FR", basis), T, nuc_unavail_daily=nuc_unavail)}
        for z in neigh:
            zd[z] = nb_window(z, nb_stack[z], nb_nl[z], nb_res[z], zone_prices(prices, z, basis), T)
        borders = [b for b in NTC if b[0] in zd and b[1] in zd]
        # market rules effective in THIS window (IT/ES were floored at 0 before TIDE / Dec-2023)
        res_bid, price_floor = rules_at(wb, w0, list(zd))
        try:
            out = solve_with_triggers(T, zd, borders, {b: ntc[b] for b in borders}, res_schemes,
                                      res_bid=res_bid, price_floor=price_floor)
        except RuntimeError:
            continue
        price_chunks.append(out["prices"])

    model = pd.concat(price_chunks).sort_index()
    metrics = _score(model, obs, zones)
    outdir = config.reports_dir
    outdir.mkdir(parents=True, exist_ok=True)
    lake.write_table(model, "dispatch", "backtest_prices", year=year)
    metrics.to_csv(outdir / f"backtest_{year}_metrics.csv", index=False)   # CSV = human export (§6)
    return {"model_prices": model, "observed": obs, "metrics": metrics}


def _score(model, obs, zones) -> pd.DataFrame:
    rows = []
    for z in zones:
        o = obs.get(z)
        if o is None:
            continue
        m = model[z]
        idx = m.index.intersection(o.index)
        m, o = m.reindex(idx), o.reindex(idx)
        ok = m.notna() & o.notna()
        m, o = m[ok], o[ok]
        if len(m) < 100:
            continue
        rows.append({
            "zone": z, "hours": len(m),
            "model_mean": round(m.mean(), 1), "obs_mean": round(o.mean(), 1),
            "baseload_err_pct": round(100 * (m.mean() - o.mean()) / o.mean(), 1),
            "corr": round(float(np.corrcoef(m, o)[0, 1]), 3),
            "P5_err": round(m.quantile(.05) - o.quantile(.05), 1),
            "P50_err": round(m.quantile(.50) - o.quantile(.50), 1),
            "P95_err": round(m.quantile(.95) - o.quantile(.95), 1),
            "neg_hrs_model": int((m < 0).sum()), "neg_hrs_obs": int((o < 0).sum()),
            "spike_hrs_model": int((m > 3 * o.median()).sum()), "spike_hrs_obs": int((o > 3 * o.median()).sum()),
        })
    return pd.DataFrame(rows)
