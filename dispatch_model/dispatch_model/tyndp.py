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


def coverage(tyndp: dict, zone: str, ref_year: int) -> dict[str, str]:
    """Classify every projection-relevant variable for `zone` as ok / clamped / missing.

    Written because the 2024 backcast found this failing SILENTLY and expensively. A zone whose anchors
    all start after `ref_year` gets `_interp(ref_year)` clamped flat to the first anchor, so the factor
    collapses to ~1.0 and the variable is FROZEN at its reference-year level for every projected year — it
    looks like a deliberate "no change" scenario and is indistinguishable from one in the output. A zone
    absent from the tab falls back to a flat CAGR, which is a design choice but an invisible one.

    Measured cost of that silence (2024 projected from 2019, vs observed):

        CH   `cap_solar_gw`/`cap_wind_gw` anchored 2025+, no 2019 row -> res factor exactly 1.0
        NL   no RES/demand/flex rows at all                          -> CAGR +4.5 %/yr, no flex block
        IT   same as CH

        negatives  CH 292 -> 0     NL 458 -> 0     BE 404 -> 0
        mean err   CH +20.3        NL +16.4        (the whole low tail vanishes: NL p5 75 vs ~0 observed)

    `clamped` is the dangerous class: `missing` at least routes through a documented fallback, whereas a
    clamped variable silently asserts "no structural change" for 30 years. Neither can be fixed by
    measurement — CH/IT have no 2019 RES capacity at source (ENTSO-E omits Swiss solar/wind, and a
    generation proxy reads 297 MW against a ~2 GW fleet because Swiss PV is behind the meter), and NL
    needs scenario anchors. So the fix is to make them impossible to miss.
    """
    z = tyndp.get(zone) or {}
    out: dict[str, str] = {}
    for var in ("demand_twh", "cap_flex_gw", *_RES_VARS, *sorted(set(_CAP_VAR.values()))):
        s = z.get(var)
        if not s:
            out[var] = "missing"
        elif min(s) > ref_year:
            out[var] = f"clamped (anchors start {min(s)}, no {ref_year} baseline)"
        else:
            out[var] = "ok"
    return out


#: variables whose ABSENCE is worth reporting. The rest (oil, biomass, lignite, psp) are absent from the
#: tab for nearly every zone by design and route through the documented CAGR fallback, so listing them
#: buries the signal. `clamped` is reported for EVERY variable regardless — it is never intentional.
_CORE_VARS = ("demand_twh", "cap_solar_gw", "cap_wind_gw", "cap_flex_gw")


def report_coverage(tyndp: dict, zones, ref_year: int) -> list[str]:
    """Human-readable lines for every zone/variable NOT on a sound footing. Empty list = full coverage.

    `cap_flex_gw` is exempt from `clamped`: it is read as an absolute level, not a ratio, so a late first
    anchor is correct rather than degenerate. `missing` is reported only for `_CORE_VARS`.
    """
    lines = []
    for z in zones:
        cov = coverage(tyndp, z, ref_year)
        bad = {}
        for v, s in cov.items():
            if s == "ok":
                continue
            if s.startswith("clamped"):
                if v != "cap_flex_gw":
                    bad[v] = s
            elif v in _CORE_VARS:
                bad[v] = s
        if bad:
            lines.append(f"  {z}: " + ", ".join(f"{v}={s}" for v, s in sorted(bad.items())))
    return lines


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
