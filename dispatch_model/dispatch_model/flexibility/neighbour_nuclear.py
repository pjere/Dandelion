"""Neighbour-zone FLEX (post-F8) — block-level pseudo-units for BE/CH/ES nuclear with the κ commitment
floor and the fleet-operating band, per-zone calibration anchors measured from ENTSO-E/REMIT data.

Why this exists: F7's honest depth boundary. FR's observed mid-band prints (−5…−50 €/MWh) happen when the
*whole region* is in surplus — and the model's neighbours absorbed FR's exports at positive prices because
their nuclear fleets had no commitment rigidity (measured on the reference hours: model BE +5 vs observed
−11). This module gives each neighbour nuclear fleet the same mechanism FR got in F7, at the granularity
the data supports: **block-level pseudo-units** (~1 GW slices of the zone's single nuclear block — there is
no per-unit stack, no REMIT-name join, no revealed per-reactor curve abroad), with `{κ, alpha_band_op,
c_mod}` anchored on the *zone's own* measured behaviour (`scratchpad/nz_anchors.py`, same method as FR):
socle share = mean(output/available) on negative-price hours; socle bid = p5 of observed negative prices
(⇒ `c_mod = srmc − socle_bid`); fleet floor = κ·α_op. Where a zone's negative-price sample is too thin to
measure (CH pre-2024), the anchor falls back to the FR-measured value, flagged in `_ANCHORS`.

Deliberately NOT ported from the FR builder: C6 reserves / C7 minstab (French regime mechanisms),
C4 maneuverability (needs per-unit outage calendars), revealed-curve bid grading (pseudo-units bid the
zone's flat nuclear SRMC; the ε tie-break separates them). The solver machinery is already zone-agnostic
(`flex = {zone: spec}`) — this is purely a spec-builder.

DE thermal (2024 has no German nuclear) gets its rigidity via `run_backtest(de_unit_level=True)` (MaStR
unit stack, #73) + the generic §4 fossil append (`fr_nuclear._append_fossil`), wired in `run_backtest`.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import reactor_class as rc

#: per-zone anchors — MEASURED 2026-07-28 by `scratchpad/nz_anchors.py` (the FR method: socle share =
#: mean(output/available) on observed-negative hours; socle bid = p5 of observed negative prices; fleet
#: floor = κ·α_op). The neighbour fleets are far more rigid than FR's (the only fleet that load-follows):
#: BE/CH/ES/DE run ~0.96–0.99 of available THROUGH deep negatives — baseload-only, no mode-G.
_ANCHORS: dict[str, dict] = {
    # zone: {kappa, alpha_op, socle_bid, source}  —  κ·α_op ≈ the measured fleet floor
    "BE": {"kappa": 0.99, "alpha_op": 0.97, "socle_bid": -55.0,
           "source": "measured 2023+2024: socle 0.96/0.97, p5 −52/−60, floor 0.98/0.99"},
    "CH": {"kappa": 0.99, "alpha_op": 0.98, "socle_bid": -70.0,
           "source": "measured 2023+2024: socle 0.98/0.99, p5 −64/−75, floor 0.98/1.00"},
    "ES": {"kappa": 0.98, "alpha_op": 0.96, "socle_bid": -60.0,
           "source": "floor measured 2024 (socle 0.95, floor 0.98); bid depth MARKET-CENSORED at −1.9 "
                     "(negatives legal only since 2023-12, all shallow) → BE/CH value borrowed"},
    "DE_LU": {"kappa": 0.99, "alpha_op": 0.98, "socle_bid": -67.0,
              "source": "measured 2019 (socle 1.19 clipped to 1.0 — REMIT/installed mismatch, same artefact "
                        "as FR's >1 tranche); fleet closed 2023-04, spec only attaches pre-2023"},
}

_PSEUDO_MW = 1000.0            # pseudo-unit slice — reactor-scale, so class physics map by capacity


def split_nuclear_block(stack: pd.DataFrame, zone: str) -> pd.DataFrame:
    """Replace the zone's single `nuclear` block row with ~1 GW pseudo-units (equal slices, same SRMC).

    Block-level zones carry one `{zone}_nuclear` row; a single block cannot express partial-fleet
    commitment (κ<1 shutting *some* units) at LP granularity, and per-unit physics (class by capacity)
    need reactor-scale rows. Returns the stack unchanged when the zone has no nuclear row."""
    st = stack.reset_index(drop=True)
    m = st["tech"].to_numpy() == "nuclear"
    if not m.any():
        return st
    total = float(st.loc[m, "capacity_mw"].sum())
    n = max(1, int(round(total / _PSEUDO_MW)))
    proto = st.loc[m].iloc[0]
    rows = []
    for i in range(n):
        r = proto.copy()
        r["unit_id"] = f"{zone}_nuc_pu{i}"
        r["capacity_mw"] = total / n
        rows.append(r)
    out = pd.concat([st.loc[~m], pd.DataFrame(rows)], ignore_index=True)
    return out


def build_fossil_flex_spec(stack: pd.DataFrame, fossil_c_start: dict) -> dict | None:
    """§4 fossil commitment as a standalone spec (for zones with no nuclear to append to — DE_LU 2024):
    min stable load (`α_band = α_tech = α_min` ⇒ no deep band), tech start cost, fast recommit, real
    per-tech up-ramp. Mirrors `fr_nuclear._append_fossil`; None when the stack has no fossil rows."""
    from .fr_nuclear import _FOSSIL_MIN_LOAD, _FOSSIL_RHO_RECOMMIT, _FOSSIL_TECHS
    st = stack.reset_index(drop=True)
    fos = np.flatnonzero(st["tech"].isin(_FOSSIL_TECHS).to_numpy())
    if fos.size == 0:
        return None
    techs = st.loc[fos, "tech"].to_numpy()
    amin = np.asarray([_FOSSIL_MIN_LOAD[t] for t in techs], float)
    cs = np.asarray([float(fossil_c_start.get(f"c_start_{t}", 30.0)) for t in techs], float)
    r_up = (np.clip(st.loc[fos, "ramp_frac"].to_numpy(float), 0.05, 1.0)
            if "ramp_frac" in st.columns else np.ones(fos.size))
    z = np.zeros(fos.size)
    return {"idx": fos, "is_nuclear": np.zeros(fos.size, bool),
            "alpha_band": amin, "alpha_tech": amin, "c_mod": 45.0, "c_start": cs,
            "d_max_8h": z, "d_max_day": z, "r_up": r_up, "xenon_beta": z,
            "rho_recommit": np.full(fos.size, _FOSSIL_RHO_RECOMMIT), "u_min_frac": z,
            "deepband_scale": np.ones(fos.size), "must_run_frac": z}


def build_neighbour_flex_spec(stack: pd.DataFrame, zone: str, c_start_by_class: dict) -> dict | None:
    """→ flex spec for a neighbour zone's (pseudo-unit) nuclear rows, or None without nuclear.

    Same constraint families as FR's F7 core — κ commitment floor, two-tier operating band, C2 budgets,
    C3 xénon (β auto-clamped to its ceiling), C5 start + min-down — with the zone's `_ANCHORS`:
    `alpha_op` overrides the class band floor, `c_mod = srmc − socle_bid` (the zone's measured implied
    modulation cost). No C6/C7, no maneuverability, no reserve_idx (see module docstring)."""
    st = stack.reset_index(drop=True)
    nuc = np.flatnonzero(st["tech"].to_numpy() == "nuclear")
    if nuc.size == 0:
        return None
    a = _ANCHORS.get(zone, _ANCHORS["BE"])
    caps = st.loc[nuc, "capacity_mw"].to_numpy(float)
    # nuclear SRMC is the flat fuel-cost proxy (commodity prices don't move it), and the preload stacks
    # don't carry `srmc_eur_mwh` yet (nb_window prices them per window) — so use the proxy directly.
    from ..stacks.costs import nuclear_srmc
    srmc = np.full(nuc.size, float(nuclear_srmc()))
    phys = [rc.physics(capacity_mw=c) for c in caps]

    def _col(k):
        return np.asarray([p[k] for p in phys], float)

    ab = np.maximum(_col("alpha_band"), float(a["alpha_op"]))
    at = _col("alpha_tech")
    r_up = _col("r_up")
    beta = np.minimum(_col("xenon_beta"), r_up / np.maximum(8.0 * (ab - at), 1e-9))   # the F7 β ceiling
    c_mod = float(np.mean(srmc) - float(a["socle_bid"]))          # zone-measured implied modulation cost
    classes = [rc.class_name(capacity_mw=c) for c in caps]
    c_start = np.asarray([float(c_start_by_class.get(f"c_start_{cl}", 320.0)) for cl in classes], float)
    return {"idx": nuc, "is_nuclear": np.ones(nuc.size, bool),
            "alpha_band": ab, "alpha_tech": at, "c_mod": c_mod, "c_start": c_start,
            "d_max_8h": _col("d_max_8h"), "d_max_day": _col("d_max_day"),
            "r_up": r_up, "xenon_beta": beta, "rho_recommit": _col("rho_recommit"),
            "u_min_frac": np.full(nuc.size, float(a["kappa"])),
            "deepband_scale": np.ones(nuc.size), "must_run_frac": np.zeros(nuc.size)}
