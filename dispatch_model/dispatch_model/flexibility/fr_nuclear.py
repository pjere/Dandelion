"""FLEX-F2b — the real per-reactor FR nuclear stack and its LP rigidity spec.

When the FLEX module is on, the FR nuclear fleet is **not** collapsed into the ~6-tranche revealed-curve
surrogate (``stacks.nuclear_curve.expand_stack``): the intertemporal rigidities (C1/C2/C3/C5) act on
*individual reactors* — a tranche has no commitment state ``u``, no fuel-cycle position, no start to pay
for — so the per-reactor rows must survive. This module keeps them and produces the ``flex`` spec dict that
``lp.highs_solver._build`` reads.

Two channels of nuclear supply behaviour are kept deliberately distinct (spec §1.3):

* **bids** — each reactor is priced along the **revealed** supply curve (``stacks.nuclear_curve``),
  reproducing the observed positive-price slope as fleet heterogeneity, but **floored at the fuel cost**.
  The revealed curve's sub-zero *socle* bid is not written in: FLEXIBILITY.md is explicit that nuclear's
  negative bid is *never added explicitly* — it must emerge from the rigidity. So below-zero prints come
  from the LP itself: C1 floors a committed reactor at ``α_band·u`` and pushing below that (deep-mod ``d``)
  costs ``c_mod``, so the reactor's implicit bid to keep producing the marginal MWh is ``srmc − c_mod < 0``.
  The revealed curve therefore contributes only its *positive* structure (the fuel-cost socle plus the
  flexible fraction's upward slope); its negative tail is *replaced* by the endogenous mechanism.

* **rigidity** — per-reactor-class physics (α bands, ramps, xénon β, deep-mod caps, recommit ramp) from
  ``reactor_class``, plus the year's regime costs (``c_mod``, ``c_start``) from ``trajectories``. F1
  maneuverability, when supplied, derates the deep-mod caps of reactors in end-of-cycle stretch-out
  (``reduced`` → half caps, ``none`` → no deep-mod / must-run). The full C4 maneuverability machinery
  (½-cap reduced band, must-run stretch-out power) lands in F3; F2b only *attaches* the F1 signal here.

The returned ``idx`` are positions of the nuclear rows in the returned stack. The stack must be used as-is
downstream: ``rolling.windows.fr_window`` appends DSR tranches at the end and only overwrites SRMC in place
(``apply_water_value`` = ``apply_bids``), so those positions stay valid across the weekly windows.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..stacks.costs import nuclear_srmc
from ..stacks.revealed import BID_COL
from . import reactor_class as rc

_STATE_CAP_SCALE = {"full": 1.0, "reduced": 0.5, "none": 0.0}   # F1 maneuverability → deep-mod cap derate

# §4 fossil rigidity: a committed thermal unit must run ≥ α_min·u (min stable load) and pays c_start to
# recommit. These min-loads are the physical stable-generation floors that the base FLEX table (which sets
# gas/coal `min_gen_frac`=0 for the pure LP) does not carry — they are the §4 seeds (F7 may refine).
_FOSSIL_MIN_LOAD = {"gas": 0.45, "ccgt": 0.45, "ocgt": 0.20, "coal": 0.40, "lignite": 0.50,
                    "oil": 0.40, "biomass": 0.50}
_FOSSIL_RHO_RECOMMIT = 0.50            # fossil re-commits faster than nuclear (no xénon) — hot-start in ~2 h
_FOSSIL_TECHS = tuple(_FOSSIL_MIN_LOAD)


def _reactor_bids(caps: np.ndarray, curve, floor_bid: float) -> np.ndarray:
    """Per-reactor opportunity bid (€/MWh): spread the reactors along the revealed supply curve by
    cumulative capacity share, then floor each at ``floor_bid`` (the fuel cost). Flooring is what keeps the
    negatives endogenous — the curve's sub-zero socle bid is dropped, not written into the merit order.

    Reactors are ordered largest-first (deterministic) purely to place them on the curve's share axis; the
    ordering carries no economic meaning beyond reproducing the aggregate curve shape as heterogeneity.
    """
    caps = np.asarray(caps, float)
    tr = list(getattr(curve, "tranches", None) or [])
    if caps.size == 0 or not tr or caps.sum() <= 0:
        return np.full(caps.size, float(floor_bid))
    order = np.argsort(-caps, kind="stable")
    cum = np.cumsum(caps[order]) / caps.sum()
    mid = cum - 0.5 * caps[order] / caps.sum()                 # each reactor's midpoint on the share axis
    edges = np.cumsum([s for s, _ in tr])
    bids = np.asarray([b for _, b in tr], float)
    slot = np.searchsorted(edges, mid, side="right").clip(0, len(bids) - 1)
    out = np.empty(caps.size, float)
    out[order] = np.maximum(bids[slot], float(floor_bid))
    return out


def build_flex_spec(stack: pd.DataFrame, curve, c_mod: float, c_start_by_class: dict,
                    floor_bid: float | None = None, maneuver: dict | None = None,
                    r_up_req: float = 0.0, r_down_req: float = 0.0, p_minstab: float = 0.0,
                    include_fossil: bool = False, fossil_c_start: dict | None = None):
    """→ ``(stack, flex_spec)`` for the FR zone with the FLEX module on.

    ``stack``      the FR stack with its **per-reactor** nuclear rows kept (hydro already expanded, nuclear
                   *not* collapsed). Returned with the per-reactor revealed-curve bid (floored at the fuel
                   cost) written to the ``opportunity_bid_eur_mwh`` column — the same channel the hydro water
                   value uses, so ``rolling.windows.fr_window``'s ``apply_water_value`` (= ``apply_bids``)
                   applies it *after* it recomputes the flat nuclear SRMC, instead of it being clobbered.
    ``curve``      the revealed FR nuclear ``SupplyCurve`` (``stacks.nuclear_curve.load_curve``) or None.
    ``c_mod``      €/MWh deep-modulation cost for the year (``trajectories.load_flex_costs``).
    ``c_start_by_class`` {"c_start_900": €/MW, ...} for the year (same loader).
    ``floor_bid``  fuel-cost floor for the reactor bids; defaults to ``costs.nuclear_srmc()``.
    ``maneuver``   optional {name: (state, stretch_power)} from F1 ``derive_weekly`` for this window, keyed by
                   the plant name (REMIT ``unit_name`` = fleet ``name``). ``reduced`` halves the deep-mod caps
                   and deep band (C4 ½-caps); ``none`` zeroes the band and pins output at the stretch-out power
                   (C4 must-run). Looked up against the stack's ``name`` column (falling back to ``unit_id``).
                   Absent ⇒ every reactor ``full``.
    ``r_up_req`` / ``r_down_req``  fleet reserve requirements (MW, C6) for the year; 0 ⇒ no reserve row.
    ``p_minstab``  grid-stability nuclear must-run floor (MW, C7) for the zone/year; 0 ⇒ no floor row.
    ``include_fossil``  §4: also attach commitment rigidity (min stable load + start cost + recommit ramp) to
                   the FR thermal rows, as a combined spec with ``is_nuclear`` marking the two sub-fleets so
                   the nuclear-only rows (C2/C3/C6/C7) skip the fossil units.
    ``fossil_c_start``  {"c_start_gas": …, …} start costs (€/MW) for the fossil techs (from ``trajectories``).

    Returns ``(stack, None)`` unchanged when the stack carries no nuclear rows.
    """
    st = stack.reset_index(drop=True)
    nuc = np.flatnonzero(st["tech"].to_numpy() == "nuclear")
    if nuc.size == 0:
        return stack, None
    floor_bid = float(nuclear_srmc()) if floor_bid is None else float(floor_bid)
    caps = st.loc[nuc, "capacity_mw"].to_numpy(float)
    key = st.loc[nuc, "name"].to_numpy() if "name" in st.columns else st.loc[nuc, "unit_id"].to_numpy()
    classes = [rc.class_name(capacity_mw=c) for c in caps]
    phys = [rc.physics(capacity_mw=c) for c in caps]

    st = st.copy()
    if BID_COL not in st.columns:
        st[BID_COL] = np.nan
    st.loc[nuc, BID_COL] = _reactor_bids(caps, curve, floor_bid)   # applied by fr_window's apply_bids

    def _col(k):
        return np.asarray([p[k] for p in phys], float)

    d8_full = _col("d_max_8h"); dd_full = _col("d_max_day")
    d8, dd = d8_full, dd_full
    deepband_scale = np.ones(nuc.size); must_run_frac = np.zeros(nuc.size)
    if maneuver:
        deepband_scale, must_run_frac = _maneuver_arrays(maneuver, key)
        d8 = d8_full * deepband_scale; dd = dd_full * deepband_scale   # C4: derate caps with the deep band
    c_start = np.asarray([float(c_start_by_class.get(f"c_start_{cl}", c_start_by_class.get("c_start_1300", 320.0)))
                          for cl in classes], float)
    # κ commitment floor (F7): the campaign schedule keeps the available fleet committed — the LP only
    # modulates within the band. κ<1 leaves the observed few weekend shutdowns free. Nuclear-only (fossil
    # commits economically; `_append_fossil` leaves its u_min_frac at 0).
    kappa = float(c_start_by_class.get("u_commit_frac", 0.85))
    # fleet-operating band floor (F7): free modulation stops at the revealed socle share (~0.74 of the
    # available fleet — `alpha_band_op`), not at the per-unit technical band (0.55–0.60): the fleet never
    # rides its mode-G floor simultaneously. Below the operating floor is deep-mod `d`, priced c_mod — both
    # anchored on the same revealed-curve measurement (socle share / socle bid).
    ab_op = float(c_start_by_class.get("alpha_band_op", 0.74))
    ab_, at_ = np.maximum(_col("alpha_band"), ab_op), _col("alpha_tech")
    # β ceiling (F7): the C3 ramp allowance `r_up·u − β·Σ₈d` must stay ≥ 0 at worst case, else sustained
    # deep-mod *forces* p down against the C1 band floor of a committed fleet → an infeasible "xénon death
    # spiral" the physics does not contain (xénon slows the climb, at worst to zero — it never forces output
    # down). Since Σ₈d ≤ 8·(α_band−α_tech)·u, the unit-free ceiling is β ≤ r_up/(8·deep_band); the seeds
    # exceed it, so the LP-effective β is the clamped value (auto-tracks the operating band width).
    beta_eff = np.minimum(_col("xenon_beta"), _col("r_up") / np.maximum(8.0 * (ab_ - at_), 1e-9))
    spec = {"idx": nuc, "is_nuclear": np.ones(nuc.size, bool),
            "alpha_band": ab_, "alpha_tech": at_,
            "c_mod": float(c_mod), "c_start": c_start,
            "d_max_8h": d8, "d_max_day": dd,
            "r_up": _col("r_up"), "xenon_beta": beta_eff,
            "rho_recommit": _col("rho_recommit"), "u_min_frac": np.full(nuc.size, kappa),
            "deepband_scale": deepband_scale, "must_run_frac": must_run_frac,
            # private (solver ignores): the nuclear names + full deep-mod caps, so `window_spec` can re-derate
            # for a given week's maneuverability without a full rebuild.
            "_nuc_names": key, "_d8_full": d8_full, "_dd_full": dd_full}

    if include_fossil:                                            # §4: append the FR thermal commitment sub-fleet
        _append_fossil(st, spec, fossil_c_start or {})
    reserve_idx = np.flatnonzero(st["tech"].isin(("hydro_reservoir", "hydro_psp")).to_numpy())
    spec["reserve_idx"] = reserve_idx                             # C6 extra reserve providers (hydro headroom)
    spec["r_up_req"] = float(r_up_req); spec["r_down_req"] = float(r_down_req)
    spec["p_minstab"] = float(p_minstab)
    return st, spec


def _maneuver_arrays(maneuver: dict, names) -> tuple:
    """(deepband_scale, must_run_frac) per nuclear unit from a {name: (state, stretch_power)} map. `reduced`
    halves the deep band (and caps); `none` zeroes it and returns the stretch-out power as the must-run pin."""
    states = [maneuver.get(n, ("full", 1.0)) for n in names]
    scale = np.asarray([_STATE_CAP_SCALE.get(s[0], 1.0) for s in states], float)
    mrf = np.asarray([float(s[1]) if s[0] == "none" else 0.0 for s in states], float)
    return scale, mrf


def window_spec(base: dict, states: dict | None) -> dict:
    """A per-window copy of a full-year `base` spec with the nuclear units re-derated for `states`
    ({name: (maneuverability, stretch_power)} for this week). Cheap: only the C4 fields change; bids, physics,
    fossil, reserves and minstab are carried over. `states` falsy ⇒ `base` returned unchanged (all `full`)."""
    if not states:
        return base
    names = base["_nuc_names"]; nn = names.size
    scale, mrf = _maneuver_arrays(states, names)
    out = dict(base)
    for k, new_nuc in (("deepband_scale", scale), ("must_run_frac", mrf),
                       ("d_max_8h", base["_d8_full"] * scale), ("d_max_day", base["_dd_full"] * scale)):
        arr = np.array(base[k], float); arr[:nn] = new_nuc; out[k] = arr   # nuclear occupy the first nn entries
    return out


def tail_state(flex_out_z: dict) -> dict:
    """Window-seam state (F5) from a solved window's flex primal (`out["flex"][zone]`), to carry into the
    next window as fixed parameters: `u_init`/`p_init` = committed capacity / output at the last hour (hour
    −1 of the next window), `d_hist[:,0..7]` = deep-mod of the last 8 hours reversed (d_{-1} … d_{-8}) for
    the rolling-8h budget and the xénon lookback. Aligns positionally with the next window's flex units
    (same stack, same order)."""
    u = np.asarray(flex_out_z["u"], float); p = np.asarray(flex_out_z["p"], float)
    d = np.asarray(flex_out_z["d"], float)
    mu, k = u.shape[0], min(8, d.shape[1])
    d_hist = np.zeros((mu, 8))
    d_hist[:, :k] = d[:, -1:-k - 1:-1]                     # reversed last k hours: d_hist[:,0]=d_{-1}
    return {"u_init": u[:, -1].copy(), "p_init": p[:, -1].copy(), "d_hist": d_hist}


def _append_fossil(st: pd.DataFrame, spec: dict, fossil_c_start: dict) -> None:
    """Extend the (nuclear) ``spec`` in place with the FR fossil rows (§4). Each fossil unit gets
    ``alpha_band = alpha_tech = α_min`` (so its deep band is 0 → ``d`` pinned to 0: no modulation, just a
    min-stable-load floor when committed), a tech start cost, and a fast recommit ramp. ``is_nuclear`` is
    False for these, so the nuclear-only families (C2/C3/C6/C7) skip them; only C1 (min load), C5 (start),
    and min-down bite. Nothing is done when the stack has no fossil rows."""
    fos = np.flatnonzero(st["tech"].isin(_FOSSIL_TECHS).to_numpy())
    if fos.size == 0:
        return
    techs = st.loc[fos, "tech"].to_numpy()
    amin = np.asarray([_FOSSIL_MIN_LOAD[t] for t in techs], float)
    cs = np.asarray([float(fossil_c_start.get(f"c_start_{t}", fossil_c_start.get("c_start_gas", 30.0)))
                     for t in techs], float)
    # fossil up-ramp = the stack's per-tech ramp fraction (gas ~1.0, coal ~0.4); β=0 (no xénon). r_up must be
    # >0 or the shared C3 row (built for every flex unit) would forbid any up-ramp.
    if "ramp_frac" in st.columns:
        r_up = np.clip(st.loc[fos, "ramp_frac"].to_numpy(float), 0.05, 1.0)
    else:
        r_up = np.ones(fos.size)
    z = np.zeros(fos.size)
    spec["idx"] = np.concatenate([spec["idx"], fos])
    spec["is_nuclear"] = np.concatenate([spec["is_nuclear"], np.zeros(fos.size, bool)])
    spec["alpha_band"] = np.concatenate([spec["alpha_band"], amin])
    spec["alpha_tech"] = np.concatenate([spec["alpha_tech"], amin])   # band == tech ⇒ no deep-mod room
    spec["c_start"] = np.concatenate([spec["c_start"], cs])
    spec["d_max_8h"] = np.concatenate([spec["d_max_8h"], z])
    spec["d_max_day"] = np.concatenate([spec["d_max_day"], z])
    spec["r_up"] = np.concatenate([spec["r_up"], r_up])
    spec["xenon_beta"] = np.concatenate([spec["xenon_beta"], z])
    spec["rho_recommit"] = np.concatenate([spec["rho_recommit"], np.full(fos.size, _FOSSIL_RHO_RECOMMIT)])
    spec["u_min_frac"] = np.concatenate([spec["u_min_frac"], z])      # fossil commits economically (no floor)
    spec["deepband_scale"] = np.concatenate([spec["deepband_scale"], np.ones(fos.size)])
    spec["must_run_frac"] = np.concatenate([spec["must_run_frac"], z])
