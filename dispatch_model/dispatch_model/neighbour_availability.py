"""Stochastic neighbour availability from REMIT (#80) for the 20-year projection.

The dispatch neighbour blocks size firm capacity as **p99-of-observed-generation** — already an availability
proxy for the *central* level. What the projection lacks is per-draw **variability**: a crisis / cold-snap
year where a neighbour thermal fleet is markedly less available than average (2022 FR nuclear being the
canonical case). This module turns the REMIT-observed year-to-year availability spread
(``pricemodeling.entsoe.unavailability.zone_availability_stats``) into a per-tech **multiplier around 1.0** —
mean-preserving, so it does **not** double-count the p99 proxy — that the projection applies to neighbour
firm capacity on each Monte-Carlo draw. Full unit-level neighbour outages would be a further refinement; a
fleet-level per-tech factor suffices for the aggregated neighbour blocks.
"""
from __future__ import annotations

import numpy as np

# REMIT plant_type (ENTSO-E PSR label) → dispatch stack tech
_REMIT_TO_TECH = {
    "Nuclear": "nuclear", "Fossil Gas": "gas", "Fossil Hard coal": "coal",
    "Fossil Brown coal/Lignite": "lignite", "Fossil Oil": "oil", "Biomass": "biomass",
    "Hydro Water Reservoir": "hydro_reservoir", "Hydro Run-of-river and poundage": "hydro_ror",
    "Hydro Pumped Storage": "hydro_psp",
}


def availability_multipliers(stats: dict, rng: np.random.Generator,
                             lo: float = 0.6, hi: float = 1.15) -> dict[str, float]:
    """Per-tech availability MULTIPLIER (≈1.0) from REMIT stats ``{plant_type: {mean_avail, std_avail}}``.

    Draws availability ~ N(mean, std) then divides by mean → a **mean-preserving** spread (E≈1), maps the
    plant_type to the dispatch tech, and clips. A tech with ~zero observed spread returns ≈1.0."""
    out = {}
    for pt, s in stats.items():
        tech = _REMIT_TO_TECH.get(pt)
        if tech is None or s.get("mean_avail", 0) <= 0:
            continue
        drawn = rng.normal(s["mean_avail"], s.get("std_avail", 0.0))
        out[tech] = float(np.clip(drawn / s["mean_avail"], lo, hi))
    return out


def apply_multipliers(stack, mult: dict[str, float]):
    """Return a copy of a neighbour stack with firm ``capacity_mw`` scaled by the per-tech multiplier."""
    st = stack.copy()
    for tech, m in mult.items():
        rows = st["tech"] == tech
        if rows.any():
            st.loc[rows, "capacity_mw"] *= m
    return st


def load_zone_stats(zones: list[str], years: list[int]) -> dict:
    """{zone: {plant_type: {mean_avail, std_avail, installed_mw, n_years}}} from REMIT (empty for zones with
    no REMIT data, e.g. before the neighbour backfill). Slow (per-year reconstruction) → load once and reuse."""
    from pricemodeling.config import load_settings
    from pricemodeling.db import get_engine
    from pricemodeling.entsoe.unavailability import zone_availability_stats
    eng = get_engine(load_settings().db_url)
    out = {}
    for z in zones:
        try:
            out[z] = zone_availability_stats(eng, z, years)
        except Exception:  # noqa: BLE001 — a zone with no REMIT rows just gets no stochastic availability
            out[z] = {}
    return out
