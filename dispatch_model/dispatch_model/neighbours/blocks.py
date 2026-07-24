"""Aggregated dispatchable stack + net load per neighbour zone (backtest = ENTSO-E actuals).

Each foreign zone is represented by technology blocks (not unit-level): nuclear / lignite / coal / gas /
oil / biomass / hydro reservoir / PSP. Thermal blocks are split into a few efficiency sub-blocks so the
zone's supply curve has a realistic slope (fuel-switching, e.g. German gas↔lignite, emerges from
relative SRMC). Capacity is proxied by a high quantile of observed generation (≈ available capacity in
the period); a workbook/TYNDP capacity override replaces this for projection.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import Config
from ..io.entsoe_hist import load_demand_hist, load_generation_hist, load_installed_capacity
from ..stacks.costs import EFF_RANGE
from ..stacks.fr_stack import FLEX

_DISPATCHABLE = ["nuclear", "lignite", "coal", "gas", "oil", "biomass",
                 "hydro_reservoir", "hydro_psp"]
_MUSTTAKE = ["solar", "wind_onshore", "wind_offshore", "hydro_ror"]
_THERMAL = {"gas", "coal", "lignite", "oil", "biomass"}
# installed → available derating (nameplate is not all dispatchable at once: outages/maintenance)
_AVAIL_FACTOR = {"nuclear": 0.78, "gas": 0.90, "coal": 0.88, "lignite": 0.90, "oil": 0.85,
                 "biomass": 0.82, "hydro_reservoir": 1.0, "hydro_psp": 0.9}
# fallback must-run shares if the workbook tab is absent (see `dispatch_must_run`)
_MUST_RUN_DEFAULT = {"lignite": 0.45, "coal": 0.35, "gas": 0.15, "biomass": 0.50, "oil": 0.0}

# --- aggregate (virtual) zones ---------------------------------------------------------------
# DE-LU's out-of-model neighbours, as FOUR price-responsive clusters rather than one DE_REST block.
# A single DE_REST bought back DE-LU's missing ~10.2 GW of export headroom (measured p99.5 of flows to
# NL/AT/DK/PL/CZ) and unblocked the DE-coincident negative-price contagion into FR/BE/CH. But lumping
# five real bidding zones into one sink hid the fact that they are **not in the same mode at the same
# hours**: when DE is in a wind glut, NL may still be importing while PL/CZ export — the aggregate net
# load never saturates (+41 GW, 0 % surplus hours in 2024), so DE-LU's surplus met a sink that was
# always willing, and the regional negatives (#138) still under-fired. Splitting restores each cluster's
# own tightness *and* its own out-of-DE borders, which the aggregate could not carry:
#   NL    ── DE-LU, BE                     (NL borders both)
#   DK    ── DE-LU                         (DK_1 west + DK_2 east, both to DE)
#   PL_CZ ── DE-LU                         (Poland + Czechia)
#   AT_SI ── DE-LU, CH, IT_NORTH           (Austria + Slovenia; closes the missing Alpine borders, #141)
# The AT_SI cluster is what makes CH↔AT and IT_NORTH↔AT/SI exist at all — the 7-zone set amputated them,
# over-tightening CH and IT-North. Each cluster is still price-responsive (own demand/RES/stack), so the
# projection stays valid; none carries an observed spot series, so none is ever scored.
ZONE_AGGREGATES = {
    "NL": ["NL"],
    "DK": ["DK_1", "DK_2"],
    "PL_CZ": ["PL", "CZ"],
    "AT_SI": ["AT", "SI"],
}


def constituents(zone: str) -> list[str]:
    """Real ENTSO-E zones behind a (possibly virtual) dispatch zone."""
    return ZONE_AGGREGATES.get(zone, [zone])


# Seasonal CHP heat-utilisation shape (share of CHP-electrical capacity that is heat-obligated by month).
# German district-heat + industrial CHP runs near-full in the heating season and backs off in summer;
# `chp_el` is a *capacity*, so the hourly must-run floor is chp_el × this shape. Applying CHP flat
# year-round is what over-forces summer and sags the annual price level. HDD-shaped default, editable.
_MONTHLY_HEAT = {1: 0.90, 2: 0.88, 3: 0.75, 4: 0.55, 5: 0.38, 6: 0.28,
                 7: 0.25, 8: 0.26, 9: 0.40, 10: 0.60, 11: 0.80, 12: 0.90}


def heat_factor(month: int) -> float:
    return _MONTHLY_HEAT.get(int(month), 0.5)


@lru_cache(maxsize=8)
def _measured_chp_mw(zone: str) -> tuple:
    """Measured CHP-electrical capacity per tech (MW) for `zone`, from the reference registry.

    This is the must-run *capacity* (unit-level MaStR, `_allocate_chp`); the window scales it by
    `heat_factor(month)`. Returns () when no registry slice exists for the zone (⇒ fall back to the
    workbook must-run fractions). Registry coverage is currently DE_LU only.
    """
    try:
        from powersim_core import registry
        reg = registry.read(zone=zone)
    except (FileNotFoundError, KeyError, ValueError):
        return ()
    if reg is None or reg.empty or "chp_el_mw" not in reg:
        return ()
    reg = reg[reg["status"].astype(str).str.contains("Betrieb", na=False)]
    chp = (reg.assign(chp=pd.to_numeric(reg["chp_el_mw"], errors="coerce"))
              .groupby("tech")["chp"].sum())
    return tuple((t, float(v)) for t, v in chp.items() if v > 0)


def measured_chp_mw(zone: str) -> dict:
    return dict(_measured_chp_mw(zone))


@lru_cache(maxsize=8)
def _must_run_rows(wb: str) -> tuple:
    """(zone, tech, frac) from the `dispatch_must_run` tab; cached per workbook path."""
    if not wb or not Path(wb).exists():
        return ()
    try:
        from powersim_core.scenario import load_sheet
        df = load_sheet(wb, "dispatch", "must_run")
    except (ValueError, KeyError):
        return ()
    return tuple((str(z), str(t), float(v))
                 for z, t, v in zip(df["zone"], df["tech"], df["must_run_frac"], strict=False))


def must_run_frac(config: Config, zone: str, tech: str) -> float:
    """Share of `tech` capacity in `zone` that cannot turn off (zone row wins over the ALL default).

    Without this the LP lets foreign thermal fleets turn down to zero, so a neighbour zone can never be
    pushed to the RES bid — the model produced **0 negative hours vs 210 observed in DE-LU (2019)**. The
    technical minimum / CHP obligation is what makes RES surplus spill into negative prices.
    """
    wb = config.resolve(config.section("assumptions")["workbook"])
    rows = _must_run_rows(str(wb) if wb else "")
    by_zone = {(z, t): v for z, t, v in rows}
    if (zone, tech) in by_zone:
        return by_zone[(zone, tech)]
    if ("ALL", tech) in by_zone:
        return by_zone[("ALL", tech)]
    return _MUST_RUN_DEFAULT.get(tech, 0.0)


def build_neighbour_stack(config: Config, zone: str, year: int, n_subblocks: int = 3,
                          cap_quantile: float = 0.999) -> pd.DataFrame:
    """→ aggregated dispatchable stack for `zone` (block-level), capacity from observed generation.

    Capacity ≈ available capacity, proxied by a near-max quantile of observed output (p99.9): peakers
    rarely run at rated power, so a lower quantile (p99) badly undersizes the stack and manufactures false
    scarcity when the LP faces actual historical load. ENTSO-E installed capacity is the exact fix (TODO).
    """
    zs = constituents(zone)
    gen = load_generation_hist(config, year, zones=zs)
    # aggregate zones sum nameplate across constituents; per-hour generation is summed for the fallback
    installed: dict = {}
    for z in zs:
        for tech, mw in load_installed_capacity(config, z, year).items():
            installed[tech] = installed.get(tech, 0.0) + mw
    gen_by_tech = gen.groupby(["timestamp_utc", "tech"])["gen_mw"].sum().reset_index()
    rows = []
    for tech in _DISPATCHABLE:
        g = gen_by_tech.loc[gen_by_tech["tech"] == tech, "gen_mw"]
        if tech in installed:                                     # installed × availability derating
            cap = installed[tech] * _AVAIL_FACTOR.get(tech, 0.88)
        elif not g.empty:                                         # fallback: p99.9 of generation
            cap = float(g.quantile(cap_quantile))
        else:
            continue
        if cap < 50:                                              # negligible / not present in zone
            continue
        if tech in _THERMAL:
            lo, hi = EFF_RANGE.get(tech, (0.35, 0.45))
            # same floor on each sub-block ⇒ forced output = must_run_frac × cap (sub-blocks sum to cap)
            mr = must_run_frac(config, zone, tech)
            for i, eff in enumerate(np.linspace(hi, lo, n_subblocks)):   # best efficiency first
                rows.append({"unit_id": f"{zone}_{tech}_{i}", "zone": zone, "tech": tech,
                             "capacity_mw": cap / n_subblocks, "efficiency": float(eff),
                             "min_gen_frac": mr})
        else:
            mn = FLEX.get(tech, (0.0, 1.0))[0]
            rows.append({"unit_id": f"{zone}_{tech}", "zone": zone, "tech": tech,
                         "capacity_mw": cap, "efficiency": np.nan, "min_gen_frac": mn})
    return pd.DataFrame(rows)


def _vintage_efficiency(tech: str, commissioning) -> float:
    """Efficiency for a thermal unit from its tech band + vintage (newer ⇒ higher within the band). MaStR
    leaves `efficiency_est` empty, so we place the unit in EFF_RANGE[tech] by commissioning year (1970→2020
    maps low→high). Missing date ⇒ band midpoint."""
    lo, hi = EFF_RANGE.get(tech, (0.35, 0.45))
    yr = pd.to_datetime(commissioning, errors="coerce")
    if pd.isna(yr):
        return 0.5 * (lo + hi)
    return float(lo + (hi - lo) * np.clip((yr.year - 1970) / 50.0, 0.0, 1.0))


def build_de_unit_stack(config: Config, zone: str, year: int, min_mw: float = 100.0) -> pd.DataFrame:
    """Unit-level DE thermal stack from the MaStR reference registry (#73): each large plant (≥ `min_mw`)
    is an individual unit with a vintage-based efficiency, and sub-threshold units are aggregated per tech
    into one block (MaStR registers tens of thousands of tiny gas gensets — keeping them individual would
    make the LP intractable without changing the merit order). Same schema as `build_neighbour_stack`, so
    it is a drop-in with a *finer* merit order (smoother SRMC curve, better price steps / negatives). The
    per-tech CHP must-run floor is still applied downstream by `_apply_measured_mustrun` (tech labels kept)."""
    from powersim_core import registry
    reg = registry.active(registry.read(zone=zone), year)
    th = reg[reg["tech"].isin(_THERMAL | {"nuclear"})].copy()
    th["capacity_mw"] = pd.to_numeric(th["capacity_mw"], errors="coerce")
    th = th[th["capacity_mw"] > 0]
    rows = []
    big = th[th["capacity_mw"] >= min_mw]
    for i, u in enumerate(big.itertuples()):
        eff = np.nan if u.tech == "nuclear" else _vintage_efficiency(u.tech, u.commissioning_date)
        av = _AVAIL_FACTOR.get(u.tech, 0.88)                    # nameplate → available (match the block builder)
        rows.append({"unit_id": f"{zone}_{u.tech}_u{i}", "zone": zone, "tech": u.tech,
                     "capacity_mw": float(u.capacity_mw) * av, "efficiency": eff,
                     "min_gen_frac": must_run_frac(config, zone, u.tech)})
    # aggregate the sub-threshold tail per tech (capacity-weighted efficiency)
    small = th[th["capacity_mw"] < min_mw]
    for tech, g in small.groupby("tech"):
        cap = float(g["capacity_mw"].sum()) * _AVAIL_FACTOR.get(tech, 0.88)
        if cap < 50:
            continue
        eff = np.nan if tech == "nuclear" else float(np.average(
            [_vintage_efficiency(tech, c) for c in g["commissioning_date"]], weights=g["capacity_mw"]))
        rows.append({"unit_id": f"{zone}_{tech}_small", "zone": zone, "tech": tech,
                     "capacity_mw": cap, "efficiency": eff, "min_gen_frac": must_run_frac(config, zone, tech)})
    return pd.DataFrame(rows)


def neighbour_netload(config: Config, zone: str, year: int) -> pd.DataFrame:
    """→ hourly [timestamp_utc, load_mw, musttake_res_mw, netload_mw] from ENTSO-E actuals.

    Aggregate zones (DK / PL_CZ / AT_SI) sum their constituents hour-by-hour.
    """
    zs = constituents(zone)
    load = (load_demand_hist(config, year, zones=zs)
            .groupby("timestamp_utc")["load_mw"].sum())
    gen = load_generation_hist(config, year, zones=zs)
    mt = (gen[gen["tech"].isin(_MUSTTAKE)].groupby("timestamp_utc")["gen_mw"].sum())
    df = pd.DataFrame({"load_mw": load}).join(mt.rename("musttake_res_mw"))
    df["musttake_res_mw"] = df["musttake_res_mw"].fillna(0.0)
    df["netload_mw"] = (df["load_mw"] - df["musttake_res_mw"])
    return df.dropna(subset=["load_mw"]).reset_index()
