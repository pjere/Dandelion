"""Direct-`highspy` multi-zone dispatch solver — the fast path that replaces the linopy build.

`lp.multi_zone.solve_multizone` builds the identical LP through linopy's xarray machinery, which
re-aligns/merges an xarray Dataset on every `+`/`*`/`.sum()` — profiling the 20-year projection showed
~90 % of the wall-clock is that symbolic construction (`alignment._get_indexes_and_vars`), rebuilt from
scratch for **every** weekly window × §51 iteration × year, while the HiGHS solve itself is milliseconds.

This module constructs the same LP directly as a sparse column matrix and hands it to HiGHS once, so the
per-window cost collapses to (assemble arrays) + (solve). Every matrix coefficient is ±1 (a pure
balance/flow network), so assembly is a handful of vectorised numpy ops. The formulation — variables,
bounds, objective, constraints, and the dual that defines each zone's price — mirrors `solve_multizone`
exactly; validated **byte-identical** against it (the golden 2019 backtest is unchanged).
"""
from __future__ import annotations

import hashlib

import highspy
import numpy as np
import pandas as pd

_EPS_FLOW = 1e-3          # €/MWh gross-flow penalty (matches multi_zone) → removes degenerate loop flows
_EPS_TIE = 0.01          # €/MWh max SRMC tie-break perturbation (F6 dual quality); ≤ this bounds any price move
_INF = highspy.kHighsInf
#: trace every HiGHS run (wall time vs the limit actually set, and the model size). Off by default;
#: enable with DISPATCH_TRACE_SOLVES=1 or by setting this flag. Answers the only question that matters
#: when a bounded window overruns: is the solver honouring `time_limit`, or ignoring it?
_TRACE_SOLVES = False
#: DISPATCH_DUMP_LP=<dir> writes each flex-path model to <dir>/lp_<n>.mps before it is solved, keeping only
#: the last two. The pathological window cannot be reproduced offline without its model, and dumping every
#: window would cost tens of GB — but a ROLLING pair means that when a run stalls, the newest file on disk
#: IS the stalling model, ready for offline experiments (reduction limits, rule masks) that take seconds
#: instead of the ~10 min preload a live reproduction needs.
_DUMP_KEEP = 2
_dump_n = 0


def _dump_model(h) -> None:
    global _dump_n
    import os
    from pathlib import Path
    d = os.environ.get("DISPATCH_DUMP_LP")
    if not d:
        return
    p = Path(d); p.mkdir(parents=True, exist_ok=True)
    _dump_n += 1
    h.writeModel(str(p / f"lp_{_dump_n}.mps"))
    old = p / f"lp_{_dump_n - _DUMP_KEEP}.mps"
    if old.exists():
        old.unlink()


def _assert_finite(cost, col_lo, col_up, row_lo, row_up, mat,
                   gen_cols, res_cols, ens_cols, dump_cols, n) -> None:
    """Reject a model containing NaN before it reaches HiGHS, naming the block that produced it.

    Learned the expensive way. One projection window carried 168 NaN entries in the cost vector — one
    FR generation unit's whole week, a hydro tranche whose `srmc_eur_mwh` and `opportunity_bid_eur_mwh`
    were BOTH NaN, so `apply_bids` (which only overwrites where the bid is present) left the NaN in
    place. HiGHS accepts such a model and reports no error: it presolves it normally (1 s), then dual
    simplex produces `nan` objectives from iteration ~19,000, never converges because every termination
    test against a NaN is false, gives up, postsolves a non-optimal solution and RE-SOLVES the original
    full-size model — which is the runaway. IPM fails the same way (kUnknown, NaN objective, duals of
    order 1e9). The window therefore looked like an unbounded solver hang for three separate
    investigations — degeneracy, then crossover, then presolve — and cost hours of CPU on containment
    strategies for a solver that was never at fault. NaN in, garbage out; this makes it say so.

    Bounds and matrix are checked too: an infinite BOUND is legitimate (`_INF` is how free columns and
    one-sided rows are expressed) but a NaN bound or a non-finite coefficient never is.
    """
    def where(i: int) -> str:
        for z, (gbase, m, units, _tech) in gen_cols.items():
            if gbase <= i < gbase + m * n:
                j = (i - gbase) // n
                return f"zone {z} generation unit #{j} ({units[j]!r}), hour {(i - gbase) % n}"
        for z, (rbase, ntr) in res_cols.items():
            if rbase <= i < rbase + ntr * n:
                return f"zone {z} RES tranche #{(i - rbase) // n}, hour {(i - rbase) % n}"
        for z, e in ens_cols.items():
            if e <= i < e + n:
                return f"zone {z} ENS, hour {i - e}"
        for z, d in dump_cols.items():
            if d <= i < d + n:
                return f"zone {z} DUMP, hour {i - d}"
        return f"column {i} (border flow / flex / storage block)"

    for name, a, allow_inf in (("cost", cost, False), ("col_lower", col_lo, False),
                               ("col_upper", col_up, True), ("row_lower", row_lo, True),
                               ("row_upper", row_up, True), ("matrix", mat, False)):
        a = np.asarray(a, float)
        bad = np.isnan(a) if allow_inf else ~np.isfinite(a)
        if not bad.any():
            continue
        ix = np.flatnonzero(bad)
        loc = where(int(ix[0])) if name in ("cost", "col_lower", "col_upper") else f"index {ix[0]}"
        raise ValueError(
            f"LP build produced {ix.size} non-finite {name} entries — first at {loc}. "
            f"A NaN here is a data defect upstream (most often a stack row with neither "
            f"`srmc_eur_mwh` nor `{BID_COL_NAME}` set); HiGHS would accept it and stall instead of "
            f"failing, so it is rejected here.")


BID_COL_NAME = "opportunity_bid_eur_mwh"   # named here to keep the guard import-free (see stacks.revealed)


def _as_time_array(v, n: int) -> np.ndarray:
    a = np.asarray(v, float)
    return np.full(n, float(v)) if a.ndim == 0 else a


def _tie_break(units) -> np.ndarray:
    """Deterministic sub-cent SRMC perturbation per unit id, to break the dual degeneracy of identical-SRMC
    sister units that makes the balance duals (= prices) noisy (F6, spec §8). A pure function of the unit id
    (blake2b → [0, `_EPS_TIE`)), so it is reproducible across runs/windows and can never move a price by more
    than `_EPS_TIE`. Applied only when the flex module is on, so flag-off stays byte-identical."""
    return np.array([int.from_bytes(hashlib.blake2b(str(u).encode(), digest_size=6).digest(), "big")
                     / 2 ** 48 * _EPS_TIE for u in units], float)


def _tranches_for(zone, zones_data, res_bid, res_tranches, n):
    """Unify the two RES paths into a tranche list [(share, floor[n], scheme)]: the scheme-tranche supply
    curve when `res_tranches` is given, else a single synthetic tranche at the flat `res_bid`."""
    rp = np.clip(np.asarray(zones_data[zone]["res_pot"], float), 0.0, None)
    if res_tranches is not None:
        trs = res_tranches[zone]
        return [(float(t["share"]), _as_time_array(t["floor"], n), str(t["scheme"])) for t in trs], rp
    bid = float(res_bid[zone]) if isinstance(res_bid, dict) else float(res_bid)
    return [(1.0, np.full(n, bid), "res")], rp


def _build(times, zones_data, borders, ntc, res_bid, voll, price_floor, res_tranches, flex=None, storage=None):
    """Assemble the LP as HiGHS column arrays + the index maps needed to read the solution back.

    Column blocks, in order, per zone: gen(units×t), res(tranches×t), ens(t), dump(t); then per border:
    fwd(t), bwd(t); then (opt-in) per flex zone: commit u(mu×t), deep-mod d(mu×t), start su(mu×t). Rows:
    balance(zone×t) [equality, dual = price], then flex rigidity rows (opt-in), then one ≤ row per energy cap.

    `flex` (opt-in, FLEX plant-operating-rigidities, still a PURE LP so the balance duals stay prices) =
    {zone: {"idx": int[mu] positions of the flex units in the zone stack, "alpha_band": f[mu],
    "alpha_tech": f[mu], "c_mod": float €/MWh, "c_start": f[mu] €/MW, and (F2b, each optional) "d_max_8h",
    "d_max_day", "r_up", "xenon_beta", "rho_recommit" — all f[mu]}}. For each flex unit it adds a
    committed-capacity `u∈[0, avail·cap]`, a deep-modulation depth `d≥0` (cost `c_mod`), and a recommitment
    `su≥0` (cost `c_start`), with (F2a) `p ≤ u`, the two-tier minimum `p ≥ α_band·u − d` and
    `d ≤ (α_band−α_tech)·u` (C1), and `su ≥ u_t − u_{t-1}` (C5-start). The start cost makes commitment *sticky*:
    rather than shut for a short negative episode (and pay to recommit) the reactor holds `u` high, is floored
    at `α_band·u`, and — when the region is in surplus — drives the price below zero.

    F2b layers the intertemporal rationing that gives the negatives the right *depth, count and timing* — each
    added only when its spec key is present (an F2a-shaped spec stays byte-identical):
      • C2a `Σ_{k=0..7} d_{t−k} ≤ d_max_8h·cap` and C2b `Σ_{t∈day} d ≤ d_max_day·cap` — deep-mod energy
        budgets (`cap` = nominal, not availability-derated), so the fleet can't modulate arbitrarily deep for
        arbitrarily long;
      • C3 `p_t − p_{t−1} ≤ r_up·u_t − β·Σ_{k=1..8} d_{t−k}` — xénon up-ramp asymmetry: recent deep modulation
        poisons the core and caps how fast the unit can climb back;
      • C5 min-down `u_t − u_{t−1} ≤ avail·rho_recommit` — a shut unit re-commits only at a bounded ramp, a
        linear min-down-time proxy that (with `c_start`) makes riding out a short trough the cheaper option.

    F3 adds the maneuverability derates + fleet-level constraints (again opt-in per key):
      • C4 `deepband_scale` (reduced→½, none→0) scales the C1b deep band; `must_run_frac` pins a unit's gen at
        `must_run_frac·avail·cap` (the stretch-out must-run — no modulation);
      • C6 reserves — `Σ_nuc(u−p)+Σ_res(cap−p) ≥ r_up_req` (headroom) and `Σ_nuc(p−α_tech·u)+Σ_res p ≥
        r_down_req` (footroom above the technical minimum), with `reserve_idx` the extra (hydro) providers;
      • C7 `Σ_{nuc} p ≥ p_minstab` — grid-stability nuclear must-run floor.
    `is_nuclear` (default all-True) marks which flex units are nuclear, so a combined nuclear+fossil spec (§4)
    applies C2/C3/C6/C7 to the nuclear subset only.

    F5 window-seam state (opt-in via `u_init`/`p_init`/`d_hist` in a zone's spec): the previous window's tail
    carried in as fixed parameters so the intertemporal rigidities don't reset at the weekly seam. It adds the
    missing hour-0 links — C5 `su_0 ≥ u_0 − u_init`, min-down `u_0 ≤ avail_0·ρ + u_init`, C3 ramp
    `p_0 − r_up·u_0 ≤ p_init − β·Σ d_{−k}` — and charges the pre-window deep-mod `d_hist` against the first
    hours' 8h budget (C2a) and xénon lookback (C3). Absent (the first window, or off) ⇒ cold start, unchanged.
    `flex=None` (default) is byte-identical to the pure LP (golden preserved)."""
    T = pd.DatetimeIndex(times)
    n = len(T)
    zones = list(zones_data)
    zrow = {z: i * n for i, z in enumerate(zones)}          # balance row base per zone
    n_bal = len(zones) * n

    col_cost, col_lo, col_up = [], [], []
    rows, cols, vals = [], [], []                            # COO triplets for the ±1 matrix
    ncol = 0

    gen_cols: dict = {}                                     # zone -> (base, m, units, tech) for extraction
    flow_cols: dict = {}                                   # border name -> (fwd_base, bwd_base)
    bal_dual_ix = {}                                       # zone -> np.arange of balance row indices

    def add_block(cost, lo, up):
        nonlocal ncol
        base = ncol
        cost = np.asarray(cost, float).ravel()
        col_cost.append(cost)
        col_lo.append(np.asarray(lo, float).ravel()); col_up.append(np.asarray(up, float).ravel())
        ncol += cost.size
        return base

    # per-zone generation, RES tranches, ENS, DUMP
    zinfo = {}
    # index de diagnostic (cf. lp.diagnostics) : renseignes ici, jamais lus par la resolution
    res_cols, ens_cols, dump_cols, srmc_by_unit, res_schemes = {}, {}, {}, {}, {}
    floor_da = {z: (float(price_floor[z]) if isinstance(price_floor, dict) else float(price_floor)) for z in zones}
    flex_info = {}                                          # zone -> (gbase, gcap, flex_spec) for the rigidity rows
    for z in zones:
        st = zones_data[z]["stack"]
        units = st["unit_id"].to_numpy()
        m = len(units)
        cap = st["capacity_mw"].to_numpy(float)
        minf = st["min_gen_frac"].to_numpy(float)
        srmc = st["srmc_eur_mwh"].to_numpy(float)
        if flex:                                           # F6: break identical-SRMC ties for clean duals
            srmc = srmc + _tie_break(units)
        av = zones_data[z].get("avail")
        if av is None:
            avail = np.ones((m, n))
        else:
            avail = av.reindex(unit=units, time=T).fillna(0.0).transpose("unit", "time").to_numpy()
        gcap = avail * cap[:, None]                         # (m, n) upper; lower = gcap*minf
        glo = gcap * minf[:, None]
        gup = gcap                                          # gen upper (u still bounds on gcap, see below)
        fz = flex.get(z) if flex else None
        if fz is not None:
            fidx0 = np.asarray(fz["idx"], int)
            glo[fidx0] = 0.0                                # flex units: the C1 band governs the floor, not minf
            mr = fz.get("must_run_frac")                   # C4 'none' maneuverability: pin p at the stretch-out power
            if mr is not None:
                mr = np.broadcast_to(np.asarray(mr, float), (fidx0.size,))
                pin = fidx0[mr > 0]
                if pin.size:
                    gup = gcap.copy()
                    pf = mr[mr > 0][:, None] * gcap[pin]    # must-run power per (unit, t) = stretch·avail·cap
                    glo[pin] = pf; gup[pin] = pf            # p frozen (no modulation); u still free to commit
        gbase = add_block(np.repeat(srmc, n), glo, gup)
        # balance: each gen col (u,t) -> row zrow[z]+t, +1
        t_idx = np.tile(np.arange(n), m)
        rows.append(zrow[z] + t_idx); cols.append(gbase + np.arange(m * n)); vals.append(np.ones(m * n))
        gen_cols[z] = (gbase, m, units, st["tech"].to_numpy())
        srmc_by_unit[z] = srmc
        if fz is not None:
            flex_info[z] = (gbase, gcap, cap, fz)

        trs, rp = _tranches_for(z, zones_data, res_bid, res_tranches, n)
        res_schemes[z] = [sc for _sh, _f, sc in trs]
        ntr = len(trs)
        r_up = np.concatenate([share * rp for share, _f, _s in trs])
        if flex:
            # F8 dual-quality extension of the F6 tie-break: perturb NEGATIVE tranche floors by a
            # deterministic per-(zone, scheme) ε ≤ 0.008 (deeper). Fired §51 tranches all land on the same
            # `fired_floor` (−0.01) across zones — thousands of identical-cost columns whose dual degeneracy
            # stalls simplex on high-RES years (measured: one 2034 window churned >24 min; healthy windows
            # take 3 s). Exact-0.0 regulatory floors (IT/ES pre-reform) stay untouched.
            trs = [(sh, np.where(f < -1e-9, f - _tie_break([f"res:{z}:{i}:{sc}"])[0] * 0.8, f), sc)
                   for i, (sh, f, sc) in enumerate(trs)]
        r_cost = np.concatenate([f for _sh, f, _s in trs])
        rbase = add_block(r_cost, np.zeros(ntr * n), r_up)
        rows.append(zrow[z] + np.tile(np.arange(n), ntr))
        cols.append(rbase + np.arange(ntr * n)); vals.append(np.ones(ntr * n))
        res_cols[z] = (rbase, ntr)

        ebase = add_block(np.full(n, voll), np.zeros(n), np.full(n, _INF))    # ENS
        ens_cols[z] = ebase
        rows.append(zrow[z] + np.arange(n)); cols.append(ebase + np.arange(n)); vals.append(np.ones(n))
        dbase = add_block(np.full(n, -floor_da[z]), np.zeros(n), np.full(n, _INF))   # DUMP (cost = -floor)
        dump_cols[z] = dbase
        rows.append(zrow[z] + np.arange(n)); cols.append(dbase + np.arange(n)); vals.append(-np.ones(n))

        zinfo[z] = {"demand": np.asarray(zones_data[z]["demand"], float)}
        bal_dual_ix[z] = zrow[z] + np.arange(n)

    # directed cross-border flows
    bnames = [f"{a}>{b}" for a, b in borders]
    for (a, b), nm in zip(borders, bnames):
        ab = _as_time_array(ntc[(a, b)][0], n)
        ba = _as_time_array(ntc[(a, b)][1], n)
        fbase = add_block(np.full(n, _EPS_FLOW), np.zeros(n), ab)             # fwd a->b
        # balance: +fwd on b, -fwd on a
        rows.append(zrow[b] + np.arange(n)); cols.append(fbase + np.arange(n)); vals.append(np.ones(n))
        rows.append(zrow[a] + np.arange(n)); cols.append(fbase + np.arange(n)); vals.append(-np.ones(n))
        wbase = add_block(np.full(n, _EPS_FLOW), np.zeros(n), ba)             # bwd b->a
        rows.append(zrow[b] + np.arange(n)); cols.append(wbase + np.arange(n)); vals.append(-np.ones(n))
        rows.append(zrow[a] + np.arange(n)); cols.append(wbase + np.arange(n)); vals.append(np.ones(n))
        flow_cols[nm] = (fbase, wbase)

    # extra (non-balance) rows, from n_bal via a shared counter: flex rigidity rows (opt-in) first, then
    # energy caps. When flex is falsy this block is skipped → the LP is byte-identical to the pure model.
    row_lo = [np.zeros(0)]; row_up = [np.zeros(0)]
    flex_cols = {}                                          # zone -> (u_base, d_base, su_base, idx) for read-back
    xrow = n_bal
    if flex:
        day_idx = np.unique(pd.DatetimeIndex(T).normalize(), return_inverse=True)[1]   # 0..nd-1 per hour (C2b)
        for z, (gbase, gcap, cap, fz) in flex_info.items():
            fidx = np.asarray(fz["idx"], int); mu = fidx.size
            if mu == 0:                                     # empty flex spec → only the SRMC tie-break, no rows
                continue
            gc = gcap[fidx]                                 # (mu, n) available cap of the flex units
            capf = np.asarray(cap, float)[fidx]             # (mu,) nominal cap — the C2 energy-budget base
            ab = np.asarray(fz["alpha_band"], float); at = np.asarray(fz["alpha_tech"], float)
            dbs = np.broadcast_to(np.asarray(fz.get("deepband_scale", 1.0), float), (mu,))   # C4 reduced→½, none→0
            dband = np.repeat((ab - at) * dbs, n); ab_rep = np.repeat(ab, n)
            # c_mod may be a scalar (neighbour/fossil specs) or a PER-REACTOR vector. The vector form is
            # required whenever the p-column bids are spread: the deep-mod engagement threshold is
            # λ < b_j − c_mod, so a laddered bid with a scalar c_mod silently re-calibrates the negative
            # tail (measured: reactors above bid 45 deep-modulate at POSITIVE prices, withdrawing 2.4–3.8 GW
            # from the shallow-negative regime — toy-LP control with `deepband_scale=0` isolates this as the
            # entire mechanism). Broadcasting keeps the scalar path byte-identical.
            c_mod = np.broadcast_to(np.asarray(fz["c_mod"], float), (mu,))
            c_start = np.asarray(fz["c_start"], float)
            # F5 window-seam state (opt-in): the previous window's tail carried in as fixed parameters, so the
            # intertemporal rigidities stay continuous across the seam. u_init/p_init are u/p at hour −1;
            # d_hist[j] = [d_{-1}, …, d_{-8}] the deep-mod of the 8 pre-window hours (for the 8h budget + xénon).
            has_state = "u_init" in fz
            if has_state:
                u_init = np.asarray(fz["u_init"], float); p_init = np.asarray(fz["p_init"], float)
                d_hist = np.broadcast_to(np.asarray(fz["d_hist"], float), (mu, 8))
            # commitment floor (F7): u ≥ u_min_frac·avail·cap — the fleet is *scheduled* committed (EDF plans
            # the campaign; day-ahead only modulates). Without it the LP sheds u freely across the week — an
            # optimizer's fiction that suppresses the forced oversupply behind real negative prints. κ<1
            # leaves the observed few weekend shutdowns possible; the C4 'none' pin overrides upward anyway.
            umf = np.broadcast_to(np.asarray(fz.get("u_min_frac", 0.0), float), (mu,))
            ulo = (umf[:, None] * gc).ravel() if umf.any() else np.zeros(mu * n)
            ub = add_block(np.zeros(mu * n), ulo, gc.ravel())                 # u ∈ [u_min·avail·cap, avail·cap]
            db = add_block(np.repeat(c_mod, n), np.zeros(mu * n), np.full(mu * n, _INF))     # deep-mod d ≥ 0
            sb = add_block(np.repeat(c_start, n), np.zeros(mu * n), np.full(mu * n, _INF))   # start su ≥ 0
            flex_cols[z] = (ub, db, sb, fidx)
            pcols = np.concatenate([gbase + ui * n + np.arange(n) for ui in fidx])   # p of flex units (j-major)
            ucols = ub + np.arange(mu * n); dcols = db + np.arange(mu * n)
            rr = xrow + np.arange(mu * n)                                            # p − u ≤ 0
            rows.append(rr); cols.append(pcols); vals.append(np.ones(mu * n))
            rows.append(rr); cols.append(ucols); vals.append(-np.ones(mu * n))
            row_lo.append(np.full(mu * n, -_INF)); row_up.append(np.zeros(mu * n)); xrow += mu * n
            rr = xrow + np.arange(mu * n)                                            # C1a: α_band·u − d − p ≤ 0
            rows.append(rr); cols.append(ucols); vals.append(ab_rep)
            rows.append(rr); cols.append(dcols); vals.append(-np.ones(mu * n))
            rows.append(rr); cols.append(pcols); vals.append(-np.ones(mu * n))
            row_lo.append(np.full(mu * n, -_INF)); row_up.append(np.zeros(mu * n)); xrow += mu * n
            rr = xrow + np.arange(mu * n)                                            # C1b: d − (α_band−α_tech)·u ≤ 0
            rows.append(rr); cols.append(dcols); vals.append(np.ones(mu * n))
            rows.append(rr); cols.append(ucols); vals.append(-dband)
            row_lo.append(np.full(mu * n, -_INF)); row_up.append(np.zeros(mu * n)); xrow += mu * n
            for j in range(mu):                                                     # C5: su_t − u_t + u_{t-1} ≥ 0
                rr = xrow + np.arange(n - 1)
                rows.append(rr); cols.append(sb + j * n + np.arange(1, n)); vals.append(np.ones(n - 1))
                rows.append(rr); cols.append(ub + j * n + np.arange(1, n)); vals.append(-np.ones(n - 1))
                rows.append(rr); cols.append(ub + j * n + np.arange(0, n - 1)); vals.append(np.ones(n - 1))
                xrow += n - 1
            row_lo.append(np.zeros(mu * (n - 1))); row_up.append(np.full(mu * (n - 1), _INF))

            # ---- F2b rigidity families. Each is opt-in on its spec key, so an F2a-shaped spec (no key)
            #      builds byte-identical to the block above; the toy F2a tests are unaffected. ----
            # ---- rolling-window STATE (F2b reformulation) -------------------------------------------
            # C2a and C3 both need the trailing 8-hour deep-mod sum. Writing that sum out per hour makes
            # consecutive rows share 7 of their 8 terms — near-parallel rows, and a vertex with enormous
            # degeneracy: dual simplex pivots among equivalent bases without improving the objective. It is
            # latent while `d` is slack, and it bites hard once high RES forces deep modulation in half the
            # hours (measured on projected 2024: the nuclear commitment floor exceeds residual demand in
            # 4384 of 8759 hours; single windows then burned 85–121 CPU-minutes and ignored every time limit).
            # Carrying the window as a state variable instead removes the overlap at source:
            #     V_t = Σ_{k=1..8} d_{t−k},   V_t = V_{t−1} + d_{t−1} − d_{t−9}
            # C2a becomes `d_t + V_t ≤ B8` (2 nonzeros, was 8) and C3 uses `β·V_t` (4 nonzeros, was up to 11).
            # The seam collapses too: the eight `d_hist` terms that used to be scattered across the first
            # eight rows of BOTH families — the exact C3×seam interaction the bisection isolated as
            # *structural* — become one fixed initial value V_0. Mathematically identical: same feasible set
            # projected on d, so prices move only by degeneracy tie-breaking.
            need_v = ("d_max_8h" in fz) or ("r_up" in fz and np.any(
                np.broadcast_to(np.asarray(fz.get("xenon_beta", 0.0), float), (mu,))))
            vb = None
            if need_v:
                B8v = (np.asarray(fz["d_max_8h"], float) * capf if "d_max_8h" in fz
                       else np.full(mu, _INF))
                # Seam history: a window can arrive having spent more than its (possibly
                # maneuverability-tightened) budget allows. The explicit form clamped its row bounds at 0;
                # the state form must instead scale the history VECTOR, because V_0 and the terms the
                # recursion later subtracts have to stay mutually consistent — clamping V_0 alone drives
                # V_1 = V_0 + d_0 − hist[7] below its zero floor and makes the LP infeasible (caught by
                # test_f5_seam_budget_clamp_stays_feasible_when_history_exceeds_budget). Scaling preserves
                # the semantics — the budget is spent, so no deep-mod until it rolls out of the window —
                # while keeping every partial sum ≤ B8 by construction.
                if has_state:
                    hs = np.asarray(d_hist[:, :8], float)
                    tot = hs.sum(axis=1)
                    scale = np.where(tot > B8v, B8v / np.maximum(tot, 1e-9), 1.0)
                    hist8 = hs * scale[:, None]
                else:
                    hist8 = np.zeros((mu, 8))
                v0 = hist8.sum(axis=1)
                vlo = np.zeros((mu, n)); vup = np.full((mu, n), _INF)
                vlo[:, 0] = v0; vup[:, 0] = v0                       # V_0 fixed
                vb = add_block(np.zeros(mu * n), vlo.ravel(), vup.ravel())
                # recursion rows, t = 1..n−1 (equalities): V_t − V_{t−1} − d_{t−1} + d_{t−9} = −hist term
                base = xrow
                for j in range(mu):
                    tt = np.arange(1, n); r = base + j * (n - 1) + (tt - 1)
                    rows.append(r); cols.append(vb + j * n + tt); vals.append(np.ones(tt.size))
                    rows.append(r); cols.append(vb + j * n + (tt - 1)); vals.append(-np.ones(tt.size))
                    rows.append(r); cols.append(db + j * n + (tt - 1)); vals.append(-np.ones(tt.size))
                    tt9 = np.arange(9, n)                            # d_{t−9} in-window for t ≥ 9
                    if tt9.size:
                        rows.append(base + j * (n - 1) + (tt9 - 1))
                        cols.append(db + j * n + (tt9 - 9)); vals.append(np.ones(tt9.size))
                    rhs = np.zeros(n - 1)
                    if has_state:                                    # t ≤ 8 ⇒ d_{t−9} is pre-window history
                        for t in range(1, min(9, n)):
                            rhs[t - 1] = -float(hist8[j, 8 - t])     # the SAME scaled vector as V_0
                    row_lo.append(rhs); row_up.append(rhs.copy())    # equality
                xrow += mu * (n - 1)
            if "d_max_8h" in fz:                       # C2a: d_t + V_t ≤ D_max8h·cap (rolling 8 h)
                B8 = np.asarray(fz["d_max_8h"], float) * capf                        # (mu,) MWh / 8h window
                base = xrow
                for j in range(mu):
                    tt = np.arange(n); r = base + j * n + tt
                    rows.append(r); cols.append(db + j * n + tt); vals.append(np.ones(n))
                    rows.append(r); cols.append(vb + j * n + tt); vals.append(np.ones(n))
                    row_lo.append(np.full(n, -_INF)); row_up.append(np.full(n, B8[j]))
                xrow += mu * n
            if "d_max_day" in fz:                      # C2b: Σ_{t∈day} d ≤ D_max_day·cap (calendar day)
                Bday = np.asarray(fz["d_max_day"], float) * capf
                nd = int(day_idx.max()) + 1
                base = xrow
                rr = base + (np.arange(mu)[:, None] * nd + day_idx[None, :]).ravel()
                rows.append(rr); cols.append(db + np.arange(mu * n)); vals.append(np.ones(mu * n))
                for j in range(mu):
                    row_lo.append(np.full(nd, -_INF)); row_up.append(np.full(nd, Bday[j]))
                xrow += mu * nd
            if "r_up" in fz:                           # C3: p_t − p_{t−1} − R_up·u_t + β·Σ_{k=1..8} d_{t−k} ≤ 0
                r_up = np.asarray(fz["r_up"], float)
                beta = np.broadcast_to(np.asarray(fz.get("xenon_beta", 0.0), float), (mu,)).astype(float)
                base = xrow
                for j in range(mu):
                    tt = np.arange(1, n); r = base + j * (n - 1) + (tt - 1)
                    rows.append(r); cols.append(gbase + fidx[j] * n + tt); vals.append(np.ones(tt.size))       # +p_t
                    rows.append(r); cols.append(gbase + fidx[j] * n + (tt - 1)); vals.append(-np.ones(tt.size))
                    rows.append(r); cols.append(ub + j * n + tt); vals.append(np.full(tt.size, -r_up[j]))       # -Rup*u
                    if beta[j] != 0.0:                     # xénon: the trailing 8h deep-mod IS the state V_t
                        rows.append(r); cols.append(vb + j * n + tt)
                        vals.append(np.full(tt.size, beta[j]))
                    # RHS is 0 for every t: the pre-window history that used to be written into rows 1..7
                    # now lives in V_0 and propagates through the recursion.
                    row_lo.append(np.full(n - 1, -_INF)); row_up.append(np.zeros(n - 1))
                xrow += mu * (n - 1)
            if "rho_recommit" in fz:                   # C5 min-down proxy: u_t − u_{t−1} ≤ avail_t·ρ_recommit
                rho = np.broadcast_to(np.asarray(fz["rho_recommit"], float), (mu,)).astype(float)
                base = xrow
                for j in range(mu):
                    tt = np.arange(1, n); r = base + j * (n - 1) + (tt - 1)
                    rows.append(r); cols.append(ub + j * n + tt); vals.append(np.ones(tt.size))
                    rows.append(r); cols.append(ub + j * n + (tt - 1)); vals.append(-np.ones(tt.size))
                    # an outage RETURN raises the commitment floor by κ·Δavail in one hour — a *scheduled*
                    # recommissioning, not an economic recommit, so it must pass the ramp cap (else infeasible
                    # against the u_min floor at every REMIT return step).
                    up = gc[j, 1:] * rho[j] + umf[j] * np.clip(gc[j, 1:] - gc[j, :-1], 0.0, None)
                    row_lo.append(np.full(n - 1, -_INF)); row_up.append(up)
                xrow += mu * (n - 1)

            # ---- F5 seam rows at t=0: the intertemporal constraints that reference hour −1, closed against
            #      the previous window's fixed tail state (u_init/p_init/d_hist). The t=1..n−1 rows above are
            #      unchanged; here we add the missing first-hour link so commitment/ramp don't reset at seams. ----
            if has_state:
                jj = np.arange(mu)
                rr = xrow + jj                                                       # C5 start: su_0 − u_0 ≥ −u_init
                rows.append(rr); cols.append(sb + jj * n); vals.append(np.ones(mu))
                rows.append(rr); cols.append(ub + jj * n); vals.append(-np.ones(mu))
                row_lo.append(-u_init); row_up.append(np.full(mu, _INF)); xrow += mu
                if "rho_recommit" in fz:                                     # min-down: u_0 ≤ avail_0·ρ + u_init
                    rho = np.broadcast_to(np.asarray(fz["rho_recommit"], float), (mu,)).astype(float)
                    rr = xrow + jj
                    rows.append(rr); cols.append(ub + jj * n); vals.append(np.ones(mu))
                    row_lo.append(np.full(mu, -_INF))
                    # never bind below the commitment floor (a cross-seam outage return raises u_min above
                    # what u_init + the economic recommit ramp can reach — a scheduled return, let it pass)
                    row_up.append(np.maximum(gc[:, 0] * rho + u_init, umf * gc[:, 0] + 1e-6)); xrow += mu
                if "r_up" in fz:                          # C3 ramp: p_0 − r_up·u_0 ≤ p_init − β·Σ_{k=1..8} d_{−k}
                    r_up = np.asarray(fz["r_up"], float)
                    beta = np.broadcast_to(np.asarray(fz.get("xenon_beta", 0.0), float), (mu,)).astype(float)
                    rr = xrow + jj
                    rows.append(rr); cols.append(gbase + fidx * n); vals.append(np.ones(mu))       # p_0
                    rows.append(rr); cols.append(ub + jj * n); vals.append(-r_up)                  # −r_up·u_0
                    row_lo.append(np.full(mu, -_INF))
                    row_up.append(p_init - beta * d_hist[:, :8].sum(axis=1)); xrow += mu

            # ---- F3 fleet-level rows. `is_nuclear` (default all-True) marks the nuclear flex units, so a
            #      combined nuclear+fossil spec (§4) applies C6/C7 to the nuclear subset only. `reserve_idx`
            #      are extra zone-stack positions (hydro) that also carry reserve. All opt-in on their keys. ----
            nucm = np.broadcast_to(np.asarray(fz.get("is_nuclear", True), bool), (mu,))
            nucj = np.flatnonzero(nucm)
            ridx = np.asarray(fz.get("reserve_idx", []), int)
            if nucj.size and "p_minstab" in fz and float(fz["p_minstab"]) > 0:   # C7: Σ_{nuc} p ≥ P_minstab[zone]
                rr = xrow + np.arange(n)
                for j in nucj:
                    rows.append(rr); cols.append(gbase + fidx[j] * n + np.arange(n)); vals.append(np.ones(n))
                # clamp the floor to the hour's *available* nuclear (× a safety margin): the minimum-injection
                # floor can never exceed what is physically online, else the window is infeasible when outages
                # take the fleet below the nominal floor.
                avail_nuc = gcap[fidx[nucj]].sum(axis=0)                         # Σ avail·cap over nuclear, per hour
                row_lo.append(np.minimum(float(fz["p_minstab"]), 0.98 * avail_nuc))
                row_up.append(np.full(n, _INF)); xrow += n
            if nucj.size and "r_up_req" in fz and float(fz["r_up_req"]) > 0:      # C6 up: Σ_nuc(u−p)+Σ_res(cap−p) ≥ R↑
                rr = xrow + np.arange(n); off = np.zeros(n)
                for j in nucj:
                    rows.append(rr); cols.append(ub + j * n + np.arange(n)); vals.append(np.ones(n))
                    rows.append(rr); cols.append(gbase + fidx[j] * n + np.arange(n)); vals.append(-np.ones(n))
                for i in ridx:                                                    # hydro: −p col, +cap into the RHS
                    rows.append(rr); cols.append(gbase + i * n + np.arange(n)); vals.append(-np.ones(n))
                    off = off + gcap[i]
                row_lo.append(float(fz["r_up_req"]) - off); row_up.append(np.full(n, _INF)); xrow += n
            if nucj.size and "r_down_req" in fz and float(fz["r_down_req"]) > 0:  # C6 down: footroom ≥ R↓
                # footroom = headroom above the *technical minimum* α_tech·u (what the unit could still be
                # commanded down to), NOT above the deep-mod-adjusted floor — measuring against α_band·u−d
                # would let the LP raise d to fabricate footroom, incentivising deeper modulation (backwards).
                rr = xrow + np.arange(n)
                for j in nucj:
                    rows.append(rr); cols.append(gbase + fidx[j] * n + np.arange(n)); vals.append(np.ones(n))
                    rows.append(rr); cols.append(ub + j * n + np.arange(n)); vals.append(np.full(n, -at[j]))
                for i in ridx:                                                    # hydro footroom above a 0 floor
                    rows.append(rr); cols.append(gbase + i * n + np.arange(n)); vals.append(np.ones(n))
                row_lo.append(np.full(n, float(fz["r_down_req"]))); row_up.append(np.full(n, _INF)); xrow += n

    # ---- storage (opt-in, PSP + BESS — the v2 increment the CH/ES level biases named): per zone-unit s,
    #      discharge gd ∈ [0, p_dis], charge gc ∈ [0, p_ch], state-of-charge e ∈ [0, e_max]; balance +gd −gc;
    #      SoC dynamics e_t = e_{t−1} + η_ch·gc_t − gd_t/η_dis (LINEAR → still a pure LP, duals stay prices);
    #      weekly energy neutrality via e pinned to 0.5·e_max at both window ends (no seam state, v1).
    #      A small `vom` on discharge breaks arbitrage-timing degeneracy. Simultaneous charge+discharge is
    #      allowed and occasionally optimal at negative prices (burning round-trip losses to absorb more) —
    #      that is real storage behaviour, not an artifact. `storage=None` (default) ⇒ byte-identical.
    storage_cols = {}
    if storage:
        for z, sz in storage.items():
            if z not in zrow:
                continue
            pd_, pc_ = np.asarray(sz["p_dis"], float), np.asarray(sz["p_ch"], float)
            em = np.asarray(sz["e_max"], float)
            ech = np.asarray(sz["eta_ch"], float); edis = np.asarray(sz["eta_dis"], float)
            vom = np.broadcast_to(np.asarray(sz.get("vom", 0.5), float), pd_.shape)
            ns = pd_.size
            gd = add_block(np.repeat(vom, n) + _tie_break([f"st:{z}:{k}" for k in range(ns)]).repeat(n),
                           np.zeros(ns * n), np.repeat(pd_, n))
            vch = np.broadcast_to(np.asarray(sz.get("vom_ch", 0.0), float), pd_.shape)   # pumping friction
            gc = add_block(np.repeat(vch, n), np.zeros(ns * n), np.repeat(pc_, n))
            elo = np.zeros((ns, n)); eup = np.repeat(em, n).reshape(ns, n).astype(float)
            elo[:, -1] = 0.5 * em; eup[:, -1] = 0.5 * em            # end-of-window neutrality pin
            eb = add_block(np.zeros(ns * n), elo.ravel(), eup.ravel())
            for k in range(ns):                                     # balance: +gd − gc
                rows.append(zrow[z] + np.tile(np.arange(n), 1)); cols.append(gd + k * n + np.arange(n))
                vals.append(np.ones(n))
                rows.append(zrow[z] + np.arange(n)); cols.append(gc + k * n + np.arange(n))
                vals.append(-np.ones(n))
                # SoC: e_t − e_{t−1} − η_ch·gc_t + gd_t/η_dis = 0  (t ≥ 1); t=0 vs the 0.5·e_max start
                rr = xrow + np.arange(n)
                rows.append(rr); cols.append(eb + k * n + np.arange(n)); vals.append(np.ones(n))
                rows.append(rr[1:]); cols.append(eb + k * n + np.arange(n - 1)); vals.append(-np.ones(n - 1))
                rows.append(rr); cols.append(gc + k * n + np.arange(n)); vals.append(np.full(n, -ech[k]))
                rows.append(rr); cols.append(gd + k * n + np.arange(n)); vals.append(np.full(n, 1.0 / edis[k]))
                lo0 = np.zeros(n); lo0[0] = 0.5 * em[k]             # e_0 − η·gc_0 + gd_0/η = e_init
                row_lo.append(lo0); row_up.append(lo0.copy()); xrow += n
            storage_cols[z] = (gd, gc, eb, ns)

    # energy-cap rows (hydro budgets): Σ_{u∈z,tech} Σ_t gen ≤ mwh
    ecap_rows = {}
    for z in zones:
        gbase, m, _units, tech = gen_cols[z]
        for t_name, mwh in (zones_data[z].get("energy_caps") or {}).items():
            umask = np.flatnonzero(tech == t_name)
            if umask.size == 0:
                continue
            cc = (gbase + (umask[:, None] * n + np.arange(n)[None, :])).ravel()
            rows.append(np.full(cc.size, xrow)); cols.append(cc); vals.append(np.ones(cc.size))
            row_lo.append([-_INF]); row_up.append([float(mwh)])
            ecap_rows[f"{z}:{t_name}"] = xrow
            xrow += 1

    # balance RHS (equality: lower = upper = demand)
    dem = np.concatenate([zinfo[z]["demand"] for z in zones])
    row_lower = np.concatenate([dem] + row_lo)
    row_upper = np.concatenate([dem] + row_up)
    nrow = xrow

    R = np.concatenate(rows); C = np.concatenate(cols); V = np.concatenate(vals)
    _assert_finite(np.concatenate(col_cost), np.concatenate(col_lo), np.concatenate(col_up),
                   row_lower, row_upper, V, gen_cols, res_cols, ens_cols, dump_cols, n)
    return {
        "n": n, "zones": zones, "ncol": ncol, "nrow": nrow,
        "col_cost": np.concatenate(col_cost), "col_lo": np.concatenate(col_lo), "col_up": np.concatenate(col_up),
        "row_lower": row_lower, "row_upper": row_upper, "coo": (R, C, V),
        "bal_dual_ix": bal_dual_ix, "flow_cols": flow_cols, "ecap_rows": ecap_rows, "T": T,
        "gen_cols": gen_cols, "res_cols": res_cols, "ens_cols": ens_cols, "dump_cols": dump_cols,
        "srmc_by_unit": srmc_by_unit, "res_schemes": res_schemes, "flex_cols": flex_cols,
        "flex_spec": flex,          # the input flex spec (params per zone) — for the F6 debug dump
        "storage_cols": storage_cols,
    }


def _to_csc(ncol, nrow, coo):
    """COO ±1 triplets → CSC (column-wise) arrays for HiGHS."""
    from scipy import sparse
    R, C, V = coo
    m = sparse.csc_matrix((V, (R, C)), shape=(nrow, ncol))
    m.sum_duplicates()
    return m.indptr.astype(np.int32), m.indices.astype(np.int32), m.data.astype(float)


_HIGHS = None


def _get_highs():
    """One resident HiGHS instance, reused across window solves — constructing a fresh ``highspy.Highs()``
    per solve costs ~85 ms (visible once linopy is gone). ``passModel`` loads a fresh LP each call, so
    solves stay independent (cold, byte-identical); only the object-construction cost is amortised.

    Dual choice (F6, spec §8): we keep the default **dual simplex** and rely on the `_tie_break`
    ε-perturbation for well-defined duals, rather than switching to interior-point (`solver="ipm"`,
    `run_crossover="off"`). Rationale: an IPM point without crossover is *interior*, so its duals are an
    analytic-centre average that does not correspond to any basic solution — for a degenerate price LP that
    smears the marginal-unit price across ties instead of naming one, which is exactly the wrong behaviour
    for reading a marginal price off the balance dual. Simplex returns a vertex dual (a genuine marginal
    unit); the sub-cent tie-break makes that vertex unique and stable hour-to-hour (verified by the
    dual-oscillation diagnostic). So: simplex + ε, crossover moot."""
    global _HIGHS
    if _HIGHS is None:
        _HIGHS = highspy.Highs()
        _HIGHS.setOptionValue("output_flag", False)
        _HIGHS.setOptionValue("presolve", "on")
    return _HIGHS


def _solve_and_read(h, spec, price_sign, diagnose: bool = False):
    import os
    import time as _t
    _t0 = _t.monotonic()
    _status = h.run()
    if _TRACE_SOLVES or os.environ.get("DISPATCH_TRACE_SOLVES"):
        try:
            _lim = h.getOptionValue("time_limit")
            _lim = _lim[1] if isinstance(_lim, tuple) else _lim
        except Exception:                                   # noqa: BLE001
            _lim = "?"
        print(f"      · solve {_t.monotonic() - _t0:7.1f}s  limit={_lim}  "
              f"ncol={spec['ncol']} nrow={spec['nrow']}  status={h.getModelStatus()}", flush=True)
    if _status != highspy.HighsStatus.kOk or h.getModelStatus() != highspy.HighsModelStatus.kOptimal:
        raise RuntimeError(f"highs LP not optimal: {h.getModelStatus()}")
    _tread = _t.monotonic()
    sol = h.getSolution()
    rd = np.asarray(sol.row_dual, float)
    cv = np.asarray(sol.col_value, float)
    T, zones = spec["T"], spec["zones"]
    prices = pd.DataFrame({z: price_sign * rd[spec["bal_dual_ix"][z]] for z in zones}, index=T)
    if spec["flow_cols"]:
        n = spec["n"]
        bn = list(spec["flow_cols"])
        net = [cv[fb:fb + n] - cv[wb:wb + n] for (fb, wb) in spec["flow_cols"].values()]
        # long frame border-major (matches the previous melt) built directly — melt was a per-solve hotspot
        flows = pd.DataFrame({"time": list(T) * len(bn), "border": np.repeat(bn, n),
                              "flow_mw": np.concatenate(net)})
    else:
        flows = pd.DataFrame(columns=["time", "border", "flow_mw"])
    water = {k: float(-rd[r]) for k, r in spec["ecap_rows"].items()}
    out = {"prices": prices, "flows": flows, "water_values": water,
           "objective": float(h.getObjectiveValue())}
    if spec["flex_cols"]:                              # read-only: expose commit/deep-mod/start/output primal (F2b)
        n = spec["n"]
        def _p(z, fidx):                               # production of the flex units (from the gen block, j-major)
            gbase = spec["gen_cols"][z][0]
            return np.stack([cv[gbase + i * n: gbase + (i + 1) * n] for i in fidx])
        out["flex"] = {z: {"idx": fidx,
                           "u": cv[ub:ub + fidx.size * n].reshape(fidx.size, n),
                           "d": cv[db:db + fidx.size * n].reshape(fidx.size, n),
                           "su": cv[sb:sb + fidx.size * n].reshape(fidx.size, n),
                           "p": _p(z, fidx)}
                       for z, (ub, db, sb, fidx) in spec["flex_cols"].items()}
    if spec.get("storage_cols"):                       # read-only storage primal (discharge/charge/SoC)
        n = spec["n"]
        out["storage"] = {z: {"dis": cv[gd:gd + ns * n].reshape(ns, n),
                              "ch": cv[gc:gc + ns * n].reshape(ns, n),
                              "soc": cv[eb:eb + ns * n].reshape(ns, n)}
                          for z, (gd, gc, eb, ns) in spec["storage_cols"].items()}
    if _TRACE_SOLVES or os.environ.get("DISPATCH_TRACE_SOLVES"):
        print(f"      · read  {_t.monotonic() - _tread:7.1f}s", flush=True)
    if diagnose:                       # lecture seule de la solution primale (cf. lp.diagnostics)
        from .diagnostics import binding_flows, debug_hour, marginal_report
        out["diag"] = marginal_report(spec, cv, prices)
        out["diag_flows"] = binding_flows(spec, cv, {})
        out["debug"] = lambda zone, hour: debug_hour(spec, cv, prices, zone, hour)   # F6 price decomposition
    return out


# ---- per-window solve budget -------------------------------------------------------------------
# Bounds the WALL time a caller may spend on one window across every solve it triggers (the §51 fixed
# point re-solves, plus the seam→cold fallback). Unset = unbounded, i.e. the historical behaviour, so
# the backtest and the golden flag-off path are untouched unless a caller opts in.
_DEADLINE: float | None = None
_MIN_SOLVE_S = 5.0


def _budget_left() -> float:
    import time as _t
    return 1e9 if _DEADLINE is None else max(0.0, _DEADLINE - _t.monotonic())


class solve_budget:
    """Context manager: `with solve_budget(300): ...` caps the enclosed solves at 300 s in total."""

    def __init__(self, seconds: float | None):
        self.seconds = seconds

    def __enter__(self):
        import time as _t
        global _DEADLINE
        self._prev = _DEADLINE
        _DEADLINE = None if self.seconds is None else _t.monotonic() + float(self.seconds)
        return self

    def __exit__(self, *exc):
        global _DEADLINE
        _DEADLINE = self._prev
        return False


def solve_multizone_highs(times, zones_data: dict, borders: list, ntc: dict,
                          res_bid=-10.0, voll: float = 15000.0, price_floor=-500.0,
                          res_tranches: dict | None = None, price_sign: float = 1.0,
                          diagnose: bool = False, flex: dict | None = None,
                          storage: dict | None = None) -> dict:
    """Cold-build + solve one window's dispatch LP directly in HiGHS. Same contract as
    ``multi_zone.solve_multizone`` (returns per-zone prices, flows, water values, objective).

    `price_sign` maps the HiGHS row dual to the market price; -1.0 reproduces linopy's sign (validated).
    `flex` opts into the plant-operating-rigidity rows (see ``_build``); None keeps the pure LP."""
    import os as _os, time as _tt
    _tb = _tt.monotonic()
    spec = _build(times, zones_data, borders, ntc, res_bid, voll, price_floor, res_tranches, flex, storage)
    if _TRACE_SOLVES or _os.environ.get("DISPATCH_TRACE_SOLVES"):
        print(f"      · build {_tt.monotonic() - _tb:7.1f}s", flush=True)
    model = highspy.HighsModel()
    lp = model.lp_
    lp.num_col_ = spec["ncol"]; lp.num_row_ = spec["nrow"]
    lp.sense_ = highspy.ObjSense.kMinimize
    lp.col_cost_ = spec["col_cost"]; lp.col_lower_ = spec["col_lo"]; lp.col_upper_ = spec["col_up"]
    lp.row_lower_ = spec["row_lower"]; lp.row_upper_ = spec["row_upper"]
    indptr, indices, data = _to_csc(spec["ncol"], spec["nrow"], spec["coo"])
    lp.a_matrix_.format_ = highspy.MatrixFormat.kColwise
    lp.a_matrix_.start_ = indptr; lp.a_matrix_.index_ = indices; lp.a_matrix_.value_ = data
    if not flex:
        h = _get_highs()
        h.passModel(model)
        return _solve_and_read(h, spec, price_sign, diagnose)
    # FLEX path (F8 robustness): high-RES projection windows can stall dual simplex for tens of minutes
    # (pathological degeneracy — measured on a 2034 window where healthy windows take seconds). Bound the
    # solve and rescue with interior point + crossover. Runs on a FRESH Highs instance, never the resident
    # one: HiGHS's run clock is owned by the instance, so a time_limit on the long-lived resident instance
    # fires instantly once its cumulative clock exceeds the limit (measured: 0-second "kTimeLimit" failures
    # poisoning every subsequent window) — and isolation also guarantees no option leakage into the
    # flag-off/golden path.
    #
    # SEAM attempts get a SHORT simplex-only bound: the pathological window class is the C3×seam-row
    # interaction (finer bisection: value-neutralised seam variants still stall — it's the row *structure*),
    # the IPM rescue has never once rescued a seam attempt (0/12 measured across the horizon runs), and the
    # caller's cold retry solves those same windows in seconds. Burning 180+600 s on a doomed seam attempt
    # only costs wall time; 90 s = 2× the slowest healthy seam solve observed (48 s under CPU contention),
    # so a healthy window cannot silently lose its seam link to the bound.
    # A per-solve bound does NOT bound the WINDOW: the caller runs the §51 fixed point (up to `max_iter`
    # solves) and then a seam→cold fallback, so the per-window worst case multiplies out to ~40 min —
    # and crossover is known to overrun `time_limit`, so the true cost is worse (measured: one 2024
    # projection window burned 85 CPU-minutes). Once the RES baseline was fixed the projection actually
    # solves a high-RES system, which makes this degenerate window class the dominant cost rather than
    # the ~9 % tail it was. `solve_budget()` lets the caller cap the whole window; every bound below is
    # clipped to what remains of it, so a window costs at most its budget however many solves it takes.
    left = _budget_left()
    is_seam = any("u_init" in z for z in flex.values())
    h = highspy.Highs()
    h.setOptionValue("output_flag", False)
    # PRESOLVE STAYS ON — turning it off was tried and REFUTED. The hang is inside presolve (traced: a
    # `build` line with no matching `solve` line, i.e. the process sits in `h.run()` having produced no
    # simplex iteration, which is also why 90 s / 180 s / 300 s ceilings all failed to contain the same
    # window — 85, 121, 114 CPU-minutes — since `time_limit` is checked at simplex iterations, not inside
    # presolve). But `presolve="off"` under a budget was measured on the same projection and made every
    # window unsolvable: 35 consecutive windows hit the 180 s limit at kTimeLimit and were dropped, where
    # presolve-on solves the healthy ones in 0.4-11 s. Presolve is worth >20x on this model and is load
    # bearing; the cost of removing it is 52 dead windows to bound one. The pathology is presumed to be a
    # data-dependent reduction cascade on deep-surplus windows (mass fixing at bounds forces rows, which
    # fixes more columns), not a size effect — every window has identical dimensions. Containment belongs
    # in `presolve_reduction_limit` (available, default -1 = unlimited), which caps the cascade instead of
    # forbidding the phase; the limit is not set here because it has not been measured yet.
    h.setOptionValue("presolve", "on")
    h.setOptionValue("time_limit", min(90.0 if is_seam else 180.0, left))
    h.passModel(model)
    _dump_model(h)
    try:
        return _solve_and_read(h, spec, price_sign, diagnose)
    except RuntimeError:
        if is_seam:
            raise                                   # let the caller's cold retry handle it (fast, proven)
        if _DEADLINE is not None:
            # Under a budget the IPM rescue is NOT attempted. HiGHS's crossover phase overruns
            # `time_limit` and cannot be preempted from Python once entered — measured twice on the same
            # 2024 window, 85 and 121 CPU-minutes against a 300 s ceiling — so clipping its limit does not
            # bound anything. A caller that asked for a bounded window gets a fast failure instead, and
            # drops the window (counted) exactly as the backtest drops its pathological ones.
            raise
        left = _budget_left()
        if left <= _MIN_SOLVE_S:                    # budget spent — fail fast so the caller can move on
            raise
        h = highspy.Highs()                         # fresh again: discard the stalled simplex state entirely
        h.setOptionValue("output_flag", False)
        h.setOptionValue("presolve", "on")
        h.setOptionValue("solver", "ipm")
        h.setOptionValue("run_crossover", "on")
        h.setOptionValue("time_limit", min(600.0, left))
        h.passModel(model)
        return _solve_and_read(h, spec, price_sign, diagnose)
