"""RES subsidy bid stack + §51 trigger (step vii core — the negative-price mechanism).

Must-take RES is not one flat bid at −10. Each support scheme keeps a different premium at negative
prices, so a plant produces while `spot > −premium`:
  * paid-regardless (FiT, green certificates, ROCs) → deep floor (produces far below zero)
  * sliding market premium → floor ≈ −(AW − monthly market value) ≈ −€15…−€30
  * merchant / post-support / CfD → floor ≈ 0

That dispersion gives RES a real *supply curve* at negative prices (reality's mean ≈ −17, not −10).

On top sits the **§51 EEG trigger** (and the CfD 6-hour rule, GB, IT…): after N *consecutive* negative
hours the premium is cancelled, the floor jumps to ≈0, and that capacity curtails — self-limiting the
depth and count. N tightens over time (6h 2017 → 1h 2027), so it is also the projection lever. Because it
is path-dependent it cannot live inside one window LP; we solve a **fixed point**: solve → find the
consecutive-negative runs → zero the premium of triggered tranches → re-solve (2–3 iterations converge).

Volumes per scheme come from the plant registry (DE) or sourced workbook estimates; both live in the
editable `dispatch_res_schemes` tab. See docs/RES_BIDDING_DESIGN.md.
"""
from __future__ import annotations

import numpy as np

from .lp.multi_zone import solve_multizone


def load_res_schemes(workbook) -> dict[str, list[dict]]:
    """{zone: [{scheme, share, floor, trigger}]} from the `dispatch_res_schemes` tab (shares normalised)."""
    from powersim_core.scenario import load_sheet
    try:
        df = load_sheet(workbook, "dispatch", "res_schemes")
    except (ValueError, KeyError):
        return {}
    out: dict[str, list[dict]] = {}
    for zone, g in df.groupby("zone"):
        tot = g["volume_share"].sum() or 1.0
        out[str(zone)] = [{"scheme": str(r.scheme), "share": float(r.volume_share) / tot,
                           "floor": float(r.bid_floor_eur_mwh), "trigger": int(r.trigger_hours)}
                          for r in g.itertuples()]
    return out


def _neg_runlength(price: np.ndarray) -> np.ndarray:
    """Consecutive-negative-hours ending at each hour (resets to 0 on any non-negative price)."""
    out = np.zeros(len(price), int)
    run = 0
    for i, p in enumerate(price):
        run = run + 1 if p < 0 else 0
        out[i] = run
    return out


def _zone_tranches(zone, schemes, res_bid_z, n) -> list[dict]:
    """Base tranches for a zone: floored zones (regulatory 0) → one tranche at 0; else scheme tranches
    (falling back to a single merchant tranche at the zone's res_bid)."""
    if res_bid_z is not None and res_bid_z >= 0:                 # IT/ES pre-reform: no negative prices
        return [{"scheme": "floored", "share": 1.0, "floor": np.zeros(n), "trigger": 0}]
    trs = schemes.get(zone) or [{"scheme": "merchant", "share": 1.0, "floor": float(res_bid_z or -10.0),
                                 "trigger": 0}]
    return [{"scheme": t["scheme"], "share": t["share"], "trigger": t["trigger"],
             "floor": np.full(n, float(t["floor"]))} for t in trs]


def solve_with_triggers(times, zones_data, borders, ntc, schemes,
                        res_bid, price_floor, max_iter: int = 3, diagnose: bool = False) -> dict:
    """`solve_multizone` wrapped in the §51 fixed point: re-solve, zeroing premiums whose consecutive
    negative-run exceeds their trigger, until the trigger pattern stops changing.

    `diagnose` (opt-in, investigation only) fait remonter le rapport marginal par (zone, heure) de
    `lp.diagnostics` sur chaque résolution ; le dernier point fixe porte le diag renvoyé."""
    n = len(times)
    zones = list(zones_data)
    rb = {z: (res_bid.get(z) if isinstance(res_bid, dict) else res_bid) for z in zones}
    tranches = {z: _zone_tranches(z, schemes, rb[z], n) for z in zones}
    base = {z: [t["floor"].copy() for t in tranches[z]] for z in zones}   # premium floors to restore
    # sticky trigger state: once a tranche-hour loses its premium in a negative episode it stays lost.
    # Non-sticky would oscillate — zeroing a premium lifts the price to 0, which then looks non-negative
    # and would "restore" the premium next iteration. Monotone accumulation converges.
    fired = {z: [np.zeros(n, bool) for _ in tranches[z]] for z in zones}

    out = solve_multizone(times, zones_data, borders, ntc, price_floor=price_floor,
                          res_tranches=tranches, diagnose=diagnose)
    for _ in range(max_iter - 1):
        changed = False
        for z in zones:
            if rb[z] is not None and rb[z] >= 0:                 # floored zone: no trigger dynamics
                continue
            runlen = _neg_runlength(out["prices"][z].to_numpy())
            for i, t in enumerate(tranches[z]):
                if t["trigger"] >= 1:
                    new_fired = fired[z][i] | (runlen >= t["trigger"])   # premium off past N consecutive
                    if new_fired.any() and not np.array_equal(new_fired, fired[z][i]):
                        fired[z][i] = new_fired
                        t["floor"] = np.where(new_fired, 0.0, base[z][i])
                        changed = True
        if not changed:
            break
        out = solve_multizone(times, zones_data, borders, ntc, price_floor=price_floor,
                              res_tranches=tranches)
    return out
