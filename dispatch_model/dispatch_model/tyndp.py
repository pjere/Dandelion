"""TYNDP capacity trajectories (#76) — ground the projection's structural evolution in ENTSO-E/ENTSOG
TYNDP scenarios (National Trends / Distributed Energy) instead of flat per-tech CAGRs.

The `dispatch_tyndp` workbook tab holds, per zone, anchor-year values for demand and per-tech installed
capacity (`variable` ∈ {demand_twh, cap_<tech>_gw}); this module interpolates them to any projection year
and expresses them as multipliers relative to the reference year, which the projection applies to demand,
RES volume, and firm-capacity stacks. Where a zone/variable is absent the projection falls back to the CAGR
(`dispatch_projection`), so the workbook can be filled incrementally from the TYNDP data portal.
"""
from __future__ import annotations

import numpy as np

# dispatch stack tech → the TYNDP capacity variable that governs it
_CAP_VAR = {
    "nuclear": "cap_nuclear_gw", "gas": "cap_gas_gw", "coal": "cap_coal_gw", "lignite": "cap_lignite_gw",
    "oil": "cap_oil_gw", "biomass": "cap_biomass_gw", "hydro_reservoir": "cap_hydro_gw",
    "hydro_ror": "cap_hydro_gw", "hydro_psp": "cap_psp_gw",
}
# RES capacity that drives the must-take volume (wind + solar)
_RES_VARS = ("cap_wind_gw", "cap_solar_gw")


def load_tyndp(workbook) -> dict:
    """{zone: {variable: {year: value}}} from the `dispatch_tyndp` tab; {} if the tab is absent."""
    from powersim_core.scenario import load_sheet
    try:
        df = load_sheet(workbook, "dispatch", "tyndp")
    except (ValueError, KeyError):
        return {}
    out: dict[str, dict] = {}
    for r in df.itertuples():
        out.setdefault(str(r.zone), {}).setdefault(str(r.variable), {})[int(r.year)] = float(r.value)
    return out


def _interp(series: dict, year: int) -> float | None:
    """Linear-interpolate {year: value} to `year`; clamps flat to the end values outside the anchor range."""
    if not series:
        return None
    ys = np.array(sorted(series))
    vs = np.array([series[y] for y in ys], float)
    return float(np.interp(year, ys, vs))     # np.interp clamps to the end values outside [min,max]


def flex_capacity_mw(tyndp: dict, zone: str, year: int) -> float:
    """Absolute 2040-flexibility capacity (MW) for `zone`/`year` from `cap_flex_gw` — the battery + demand-
    response + H2-peaker fleet that maintains adequacy as firm thermal retires. 0 if the zone/variable is
    absent (→ the projection relies on firm + DSR only, as before). Unlike the other TYNDP variables this is
    an *absolute* level (there is no reference-year flex baseline to scale)."""
    z = tyndp.get(zone)
    v = _interp(z.get("cap_flex_gw", {}), year) if z else None
    return float(v) * 1000.0 if v else 0.0


def tyndp_factors(tyndp: dict, zone: str, target_year: int, ref_year: int) -> dict | None:
    """Multipliers (target ÷ ref) for `zone` from TYNDP: {"demand": f, "res": f, "cap": {tech: f}}.
    Returns None if the zone has no TYNDP row (→ projection uses the CAGR fallback). Per-variable, a
    missing/zero reference silently drops that factor (falls back downstream)."""
    z = tyndp.get(zone)
    if not z:
        return None

    def factor(var):
        ref = _interp(z.get(var, {}), ref_year)
        tgt = _interp(z.get(var, {}), target_year)
        return (tgt / ref) if (ref and tgt is not None and ref > 0) else None

    out: dict = {"demand": factor("demand_twh"), "cap": {}}
    for tech, var in _CAP_VAR.items():
        f = factor(var)
        if f is not None:
            out["cap"][tech] = f
    # RES volume grows with total wind+solar capacity
    res_ref = sum(v for var in _RES_VARS if (v := _interp(z.get(var, {}), ref_year)))
    res_tgt = sum(v for var in _RES_VARS if (v := _interp(z.get(var, {}), target_year)))
    out["res"] = (res_tgt / res_ref) if res_ref > 0 else None
    return out
