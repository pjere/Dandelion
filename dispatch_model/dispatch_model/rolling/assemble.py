"""Assemble per-zone LP inputs for a backtest window (FR unit-level + neighbour blocks).

Handles the double-count traps: run-of-river and solar/wind are must-take (res_pot, not dispatchable);
pumped storage is excluded from v1 dispatch (storage arbitrage is a refinement). Reservoir hydro is
bid ~0 with a weekly energy budget = the window's ACTUAL reservoir generation (so hydro energy is
anchored to history while the LP places it optimally → endogenous water value). FR nuclear availability
is a rolling-max-of-output proxy; neighbour block capacities are already an availability proxy (p99 of
observed generation), so their availability is ~1.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import xarray as xr

from ..commodities.model import CommodityModel
from ..config import Config
from ..io.fr_history import load_fr_netload
from ..neighbours.blocks import build_neighbour_stack, constituents, neighbour_netload
from ..stacks.fr_stack import build_fr_stack, srmc

# representative NTC (MW) per border (ntc_ab, ntc_ba); workbook/ENTSO-E override later
NTC = {
    ("FR", "DE_LU"): (3000, 3000), ("FR", "BE"): (4300, 2800), ("FR", "CH"): (3000, 3000),
    ("FR", "IT_NORTH"): (4350, 2650), ("FR", "ES"): (2800, 3300), ("DE_LU", "BE"): (1000, 1000),
    ("DE_LU", "CH"): (4000, 4000), ("CH", "IT_NORTH"): (4200, 1900),
    # DE-LU's out-of-model neighbours, split into four clusters (was one DE_REST block). Defaults are
    # physical interconnector capacities; `flow_derived_ntc` recomputes per backtest year from realized
    # flow (summing constituent borders), so these only bind in years lacking flow history.
    ("DE_LU", "NL"): (4250, 4250), ("BE", "NL"): (2400, 2400),          # NL cluster
    ("DE_LU", "DK"): (3500, 3500),                                       # DK_1 + DK_2 → DE
    ("DE_LU", "PL_CZ"): (2600, 2600),                                    # PL + CZ → DE
    ("DE_LU", "AT_SI"): (5400, 5400), ("CH", "AT_SI"): (1200, 1200),    # AT + SI: DE / CH / IT-North
    ("IT_NORTH", "AT_SI"): (870, 385),                                   # Brenner (AT) + IT↔SI
    # IT-North ↔ the southern Italian bidding zones (internal NORD↔CNOR border). Gives IT-North its
    # export-south demand — the missing tightness that under-priced it ~15 % in 2024 (#142).
    ("IT_NORTH", "IT_SOUTH"): (5000, 3000),
}
_EXCLUDE_DISPATCH = {"hydro_psp", "hydro_ror", "solar", "wind_onshore", "wind_offshore", "waste"}

# Zones dont les frontières sont **ré-allouées vers leurs proportions physiques, à total inchangé**.
#
# La NTC dérivée prend le p99.5 du flux *réalisé* : elle mesure l'usage, pas la capacité. Pour une zone dont
# les imports se répartissent sur plusieurs frontières dont aucune ne sature, chaque frontière est sous-lue
# individuellement — mais leur **somme** reste juste, car c'est le total simultané qui est physiquement
# contraint. Le cas type est DE→CH, lu sous son import réel (~3 600 MW observés en p99.5) parce que ce flux
# ne culmine pas quand les autres frontières suisses culminent. Depuis le split de DE_REST, CH a **quatre**
# frontières d'import ré-allouées (FR, DE-LU, IT-North et la nouvelle CH↔AT_SI) — la ré-allocation les couvre
# toutes.
#
# On corrige donc la **répartition** sans toucher au **total** : chaque frontière est portée à sa capacité
# physique (table `NTC`), puis l'ensemble est renormalisé pour retrouver le total dérivé. Plancher sans
# renormaliser a été essayé et rejeté — cela gonflait l'import simultané de CH à 9 144 MW (+61 % au-dessus
# du p99.5 observé) et son export à 11 200 MW, faisant d'elle un nœud de transit non physique qui noyait
# l'Italie (IT_NORTH −1,4 → −18 % de baseload).
_NTC_FLOOR_ZONES = frozenset({"CH"})


def _apply_ntc_floor(ntc: dict) -> dict:
    """Ré-alloue les frontières de `_NTC_FLOOR_ZONES` vers leurs proportions physiques, **à total inchangé**.

    Deux directions par zone, traitées séparément (import vers la zone, export depuis la zone) : chacune est
    portée à la capacité physique de la table `NTC`, puis mise à l'échelle pour que sa somme égale celle de
    la NTC dérivée. Le facteur de coïncidence de `flow_derived_ntc` — qui borne le transit *simultané* — est
    ainsi préservé, alors qu'un simple plancher le contournait.
    """
    out = dict(ntc)
    for z in _NTC_FLOOR_ZONES:
        borders = [b for b in NTC if z in b and b in out]
        if not borders:
            continue
        # index (border, position) des directions entrantes puis sortantes pour la zone z
        imp = [(b, 0 if b[1] == z else 1) for b in borders]
        exp = [(b, 0 if b[0] == z else 1) for b in borders]
        for legs in (imp, exp):
            derived = sum(out[b][i] for b, i in legs)
            phys = sum(NTC[b][i] for b, i in legs)
            if derived <= 0 or phys <= 0:
                continue
            k = derived / phys                       # renormalisation : conserve le total simultané
            for b, i in legs:
                pair = list(out[b])
                pair[i] = NTC[b][i] * k
                out[b] = tuple(pair)
    return out


def flow_derived_ntc(config: Config, year: int, coincident: bool = True) -> dict:
    """Effective NTC per border/direction from realized physical flow.

    Base = p99.5 of realized flow per border/direction (≈ usable transfer capability; shapes historical
    spreads). But per-border p99.5s peak at *different* times, so their sum overstates a zone's achievable
    **simultaneous** export — and that phantom headroom is exactly what let the model clear surplus that in
    reality priced negative (2019 DE: model ~16 GW export headroom vs ~14 GW simultaneous observed → far
    too few negative hours). With `coincident=True` each zone's export directions are scaled by a
    **coincidence factor** = (p99.5 of its *total simultaneous* export) / (Σ of its per-border p99.5s),
    so region-wide-surplus hours congest together as they do physically. Falls back to the flat default
    where a border's flow history is missing.
    """
    import sqlite3
    con = sqlite3.connect(config.resolve(config.section("data")["sqlite_path"]))
    try:
        df = pd.read_sql("SELECT ts_utc, series_key, value FROM entsoe_flows "
                         "WHERE ts_utc >= ? AND ts_utc < ?",
                         con, params=(f"{year}-01-01", f"{year + 1}-01-01"))
    finally:
        con.close()
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    piv = df.pivot_table(index="ts", columns="series_key", values="value").fillna(0.0)

    def flow_series(x, y):
        """Hourly x→y flow, summing constituent borders (virtual zones have no series of their own)."""
        cols = [f"{i}>{j}" for i in constituents(x) for j in constituents(y) if f"{i}>{j}" in piv.columns]
        return piv[cols].sum(axis=1) if cols else pd.Series(0.0, index=piv.index)

    base = {}
    for (a, b), (dab, dba) in NTC.items():
        va, vb = flow_series(a, b), flow_series(b, a)
        base[(a, b)] = (float(va[va > 0].quantile(0.995)) if (va > 0).sum() > 100 else float(dab),
                        float(vb[vb > 0].quantile(0.995)) if (vb > 0).sum() > 100 else float(dba))
    if not coincident:
        return _apply_ntc_floor(base)

    # per-zone export coincidence: cap Σ(border p99.5) at the p99.5 of *total simultaneous* export
    zones = {z for bd in NTC for z in bd}
    factor = {}
    for z in zones:
        neigh = [b for (a, b) in NTC if a == z] + [a for (a, b) in NTC if b == z]
        sum_cap = sum(base[(z, w)][0] if (z, w) in base else base[(w, z)][1] for w in neigh)
        total = sum((flow_series(z, w) for w in neigh), start=pd.Series(0.0, index=piv.index))
        sim = float(total[total > 0].quantile(0.995)) if (total > 0).sum() > 100 else sum_cap
        factor[z] = min(1.0, sim / sum_cap) if sum_cap > 0 else 1.0

    out = {}
    for (a, b), (ab, ba) in base.items():
        out[(a, b)] = (ab * factor.get(a, 1.0), ba * factor.get(b, 1.0))
    return _apply_ntc_floor(out)


_REGIME_NTC_MEMO: dict = {}      # (db_path, year) → result; the measurement is deterministic per year
#                                  and its full-year loads cost minutes — repeated flex backtests in one
#                                  process (tests, probes) must not re-pay it.


def regime_ntc(config: Config, year: int, base: dict) -> dict:
    """Surplus-regime border caps: {"mask": {zone: hourly bool}, "caps": {(a,b): (reg_ab, reg_ba)}}.

    The static caps over-couple exactly on surplus hours: measured 2024+2025, observed flows on the
    exporter's low-residual-load hours run at a median 0-60 % of the p99.5 cap with an at-cap share of
    0-6 %, while prices decouple across the border on 34-100 % of boundary hours — under flow-based
    market coupling the exchange domain SHRINKS when RES is high (loop flows), the exact anti-correlation
    a static cap misses. Result: the model's boundary hours are regionally synchronized (~2000 h in every
    zone, ±5 %) where reality's spread 435-1458 (`scratchpad/border_regime_caps.py`).

    Regime = the exporter's residual load (demand − must-take RES, raw data — deliberately NOT the
    flex-uplifted window inputs, so the regime definition cannot drift with model changes) below its
    year p20. Cap on regime hours = p95 of the observed flow on those hours (revealed capability: with
    prices decoupled and flows below cap, the binding constraint IS the capability). Fundamentals-only
    conditioning — projection-valid, no price leakage. Falls back to the static cap where the flow or
    residual history is thin (GB has no load data post-Brexit)."""
    from ..io.entsoe_hist import load_demand_hist, load_generation_hist
    import sqlite3
    memo_key = (config.resolve(config.section("data")["sqlite_path"]), int(year))
    if memo_key in _REGIME_NTC_MEMO:
        return _REGIME_NTC_MEMO[memo_key]
    mt_techs = ("solar", "wind_onshore", "wind_offshore", "ror")
    con = sqlite3.connect(config.resolve(config.section("data")["sqlite_path"]))
    try:
        df = pd.read_sql("SELECT ts_utc, series_key, value FROM entsoe_flows "
                         "WHERE ts_utc >= ? AND ts_utc < ?",
                         con, params=(f"{year}-01-01", f"{year + 1}-01-01"))
    finally:
        con.close()
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    piv = df.pivot_table(index="ts", columns="series_key", values="value").fillna(0.0)
    piv = piv.resample("1h").mean()

    def flow_series(x, y):
        cols = [f"{i}>{j}" for i in constituents(x) for j in constituents(y) if f"{i}>{j}" in piv.columns]
        return piv[cols].sum(axis=1) if cols else pd.Series(dtype=float)

    zones = {z for bd in NTC for z in bd}
    mask = {}
    for z in zones:
        try:
            cons = constituents(z)
            dem = load_demand_hist(config, year, zones=cons)
            g = load_generation_hist(config, year, zones=cons)
        except Exception:                             # noqa: BLE001 — no data (GB) → no regime for z
            continue
        if dem.empty or g.empty:
            continue
        d = dem.groupby("timestamp_utc")["load_mw"].sum()
        mt = (g[g["tech"].isin(mt_techs)].groupby("timestamp_utc")["gen_mw"].sum())
        resid = (d - mt.reindex(d.index).fillna(0.0)).dropna()
        if len(resid) < 1000:
            continue
        mask[z] = resid < resid.quantile(0.20)
    caps = {}
    for (a, b), (ab, ba) in base.items():
        reg_ab, reg_ba = ab, ba
        for direction, (x, y, cap_static) in enumerate((( a, b, ab), (b, a, ba))):
            m = mask.get(x)
            f = flow_series(x, y)
            if m is None or f.empty:
                continue
            fr = f.reindex(m.index[m]).dropna()
            if len(fr) >= 100:
                reg = min(float(fr.quantile(0.95)), cap_static)
                if direction == 0:
                    reg_ab = reg
                else:
                    reg_ba = reg
        caps[(a, b)] = (reg_ab, reg_ba)
    out = {"mask": mask, "caps": caps}
    _REGIME_NTC_MEMO[memo_key] = out
    return out


def regime_cap_arrays(borders: list, ntc: dict, rn: dict, T) -> dict:
    """Per-window NTC arrays: the static cap everywhere, the regime cap on the EXPORTER's surplus
    hours (per direction, per hour — the LP broadcasts scalars or arrays via `_as_time_array`)."""
    out = {}
    for (a, b) in borders:
        ab, ba = ntc[(a, b)]
        reg_ab, reg_ba = rn["caps"].get((a, b), (ab, ba))
        ma, mb = rn["mask"].get(a), rn["mask"].get(b)
        arr_ab = np.where(ma.reindex(T).fillna(False).to_numpy(), reg_ab, ab) if ma is not None else ab
        arr_ba = np.where(mb.reindex(T).fillna(False).to_numpy(), reg_ba, ba) if mb is not None else ba
        out[(a, b)] = (arr_ab, arr_ba)
    return out


def _month_prices(cm: CommodityModel, ts: pd.Timestamp) -> dict:
    pm = cm.monthly_prices(ts.year, ts.year)
    row = pm[(pm["date"].dt.month == ts.month)]
    return {c: row[row["commodity"] == c]["price"].iloc[0] for c in ["gas", "co2", "coal", "oil"]}


def _fr_inputs(config, start, end, prices, nuc_avail_mult: float = 1.0) -> dict:
    h = load_fr_netload(config, str(start), str(end)).set_index("timestamp_utc")
    T = h.index
    stack = build_fr_stack(config)
    stack = stack[~stack["tech"].isin(_EXCLUDE_DISPATCH)].reset_index(drop=True)   # ROR/PSP handled elsewhere
    # GB is not on ENTSO-E (post-Brexit) → represent the GB interconnector as border import tranches
    from .windows import GB_IMPORT_TRANCHES, price_gb_tranches  # lazy: windows imports from this module
    gb = pd.DataFrame([{"unit_id": uid, "name": uid, "tech": "import", "capacity_mw": cap,
                        "min_gen_frac": 0.0, "efficiency": np.nan, "ramp_frac": 1.0, "vom": 0.0}
                       for uid, (cap, _) in GB_IMPORT_TRANCHES.items()])
    stack = pd.concat([stack, gb], ignore_index=True)
    stack = stack.assign(srmc_eur_mwh=price_gb_tranches(stack, srmc(stack, prices).to_numpy()))
    # availability: nuclear rolling-max-of-output proxy; thermal 0.95; reservoir at capacity (budget-limited)
    nuc_cap = stack.loc[stack["tech"] == "nuclear", "capacity_mw"].sum()
    nuc_frac = np.clip(pd.Series(h["gen_nuclear_mw"].to_numpy()).rolling(72, 1).max().to_numpy()
                       / max(nuc_cap, 1) * 1.03 * nuc_avail_mult, 0, 1)
    fr = {"nuclear": nuc_frac, "gas": 0.95, "coal": 0.95, "oil": 0.95, "biomass": 0.85,
          "hydro_reservoir": 1.0, "import": 1.0}
    av = np.ones((len(stack), len(T)))
    for i, t in enumerate(stack["tech"]):
        av[i, :] = fr.get(t, 0.9)                              # scalar or per-hour array both broadcast
    avail = xr.DataArray(av, coords=[("unit", stack["unit_id"].to_numpy()), ("time", T)])
    budget = float(h["gen_hydro_reservoir_mw"].sum())                       # actual reservoir energy this window
    return {"stack": stack, "demand": h["demand_mw"].to_numpy(),
            "res_pot": h["musttake_res_mw"].to_numpy(), "avail": avail,
            "energy_caps": {"hydro_reservoir": budget}, "times": T}


def _neighbour_inputs(config, zone, start, end, year, prices, ref_times) -> dict:
    st = build_neighbour_stack(config, zone, year)
    st = st[~st["tech"].isin(_EXCLUDE_DISPATCH)].reset_index(drop=True)
    st = st.assign(srmc_eur_mwh=srmc(st, prices).to_numpy())
    nl = neighbour_netload(config, zone, year).set_index("timestamp_utc").reindex(ref_times)
    nl[["load_mw", "musttake_res_mw"]] = nl[["load_mw", "musttake_res_mw"]].interpolate().ffill().bfill()
    # reservoir weekly budget = actual reservoir generation in the window
    from ..io.entsoe_hist import load_generation_hist
    g = load_generation_hist(config, year, zones=constituents(zone))   # virtual zones sum their constituents
    if not g.empty and "tech" in g.columns:
        g = g[(g["tech"] == "hydro_reservoir")
            & (g["timestamp_utc"] >= ref_times[0]) & (g["timestamp_utc"] <= ref_times[-1])]
    else:
        g = g.iloc[0:0]
    caps = ({"hydro_reservoir": float(g["gen_mw"].sum())}
            if not g.empty and (st["tech"] == "hydro_reservoir").any() else {})
    return {"stack": st, "demand": nl["load_mw"].to_numpy(),
            "res_pot": nl["musttake_res_mw"].to_numpy(), "avail": None, "energy_caps": caps}


def assemble_window(config: Config, start, end, zones=None, price_mult=None,
                    nuc_avail_mult: float = 1.0) -> tuple:
    """→ (times, zones_data, borders, ntc) for the multi-zone LP over [start, end).

    `price_mult` scales commodity prices ({"gas":1.5} etc.) and `nuc_avail_mult` scales FR nuclear
    availability — used for the §8 projection-sensitivity checks.
    """
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    cm = CommodityModel.from_workbook(config.resolve(config.section("assumptions")["workbook"]))
    prices = _month_prices(cm, start)
    if price_mult:
        prices = {k: v * price_mult.get(k, 1.0) for k, v in prices.items()}
    zones = zones or [z for z in config.all_zones if z != "GB"]        # GB = border curve (no ENTSO-E data)

    fr = _fr_inputs(config, start.date(), end.date(), prices, nuc_avail_mult=nuc_avail_mult)
    T = fr["times"]
    zd = {"FR": {k: v for k, v in fr.items() if k != "times"}}
    for z in zones:
        if z == "FR":
            continue
        try:
            zd[z] = _neighbour_inputs(config, z, start.date(), end.date(), start.year, prices, T)
        except (KeyError, ValueError):          # zone lacks data for this year (e.g. a cluster with no 2019
            continue                            # generation) → drop it, mirroring run_backtest's neighbour loop
    borders = [b for b in NTC if b[0] in zd and b[1] in zd]
    ntc = {b: NTC[b] for b in borders}
    return T, zd, borders, ntc
