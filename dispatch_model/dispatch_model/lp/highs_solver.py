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

import highspy
import numpy as np
import pandas as pd

_EPS_FLOW = 1e-3          # €/MWh gross-flow penalty (matches multi_zone) → removes degenerate loop flows
_INF = highspy.kHighsInf


def _as_time_array(v, n: int) -> np.ndarray:
    a = np.asarray(v, float)
    return np.full(n, float(v)) if a.ndim == 0 else a


def _tranches_for(zone, zones_data, res_bid, res_tranches, n):
    """Unify the two RES paths into a tranche list [(share, floor[n], scheme)]: the scheme-tranche supply
    curve when `res_tranches` is given, else a single synthetic tranche at the flat `res_bid`."""
    rp = np.clip(np.asarray(zones_data[zone]["res_pot"], float), 0.0, None)
    if res_tranches is not None:
        trs = res_tranches[zone]
        return [(float(t["share"]), _as_time_array(t["floor"], n), str(t["scheme"])) for t in trs], rp
    bid = float(res_bid[zone]) if isinstance(res_bid, dict) else float(res_bid)
    return [(1.0, np.full(n, bid), "res")], rp


def _build(times, zones_data, borders, ntc, res_bid, voll, price_floor, res_tranches, flex=None):
    """Assemble the LP as HiGHS column arrays + the index maps needed to read the solution back.

    Column blocks, in order, per zone: gen(units×t), res(tranches×t), ens(t), dump(t); then per border:
    fwd(t), bwd(t); then (opt-in) per flex zone: commit u(mu×t), deep-mod d(mu×t), start su(mu×t). Rows:
    balance(zone×t) [equality, dual = price], then flex rigidity rows (opt-in), then one ≤ row per energy cap.

    `flex` (opt-in, FLEX-F2 plant-operating-rigidities, still a PURE LP so the balance duals stay prices) =
    {zone: {"idx": int[mu] positions of the flex units in the zone stack, "alpha_band": f[mu],
    "alpha_tech": f[mu], "c_mod": float €/MWh, "c_start": f[mu] €/MW}}. For each flex unit it adds a
    committed-capacity `u∈[0, avail·cap]`, a deep-modulation depth `d≥0` (cost `c_mod`), and a recommitment
    `su≥0` (cost `c_start`), with `p ≤ u`, the two-tier minimum `p ≥ α_band·u − d` and `d ≤ (α_band−α_tech)·u`
    (C1), and `su ≥ u_t − u_{t-1}` (C5). The start cost makes commitment *sticky*: rather than shut for a short
    negative episode (and pay to recommit) the reactor holds `u` high, is floored at `α_band·u`, and — when
    the region is in surplus — drives the price below zero. `flex=None` (default) is byte-identical to the
    pure LP (golden preserved). C2 budgets / C3 xénon / min-down persistence are added in F2b/F3."""
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
        av = zones_data[z].get("avail")
        if av is None:
            avail = np.ones((m, n))
        else:
            avail = av.reindex(unit=units, time=T).fillna(0.0).transpose("unit", "time").to_numpy()
        gcap = avail * cap[:, None]                         # (m, n) upper; lower = gcap*minf
        glo = gcap * minf[:, None]
        fz = flex.get(z) if flex else None
        if fz is not None:
            glo[np.asarray(fz["idx"], int)] = 0.0          # flex units: the C1 band governs the floor, not minf
        gbase = add_block(np.repeat(srmc, n), glo, gcap)
        # balance: each gen col (u,t) -> row zrow[z]+t, +1
        t_idx = np.tile(np.arange(n), m)
        rows.append(zrow[z] + t_idx); cols.append(gbase + np.arange(m * n)); vals.append(np.ones(m * n))
        gen_cols[z] = (gbase, m, units, st["tech"].to_numpy())
        srmc_by_unit[z] = srmc
        if fz is not None:
            flex_info[z] = (gbase, gcap, fz)

        trs, rp = _tranches_for(z, zones_data, res_bid, res_tranches, n)
        res_schemes[z] = [sc for _sh, _f, sc in trs]
        ntr = len(trs)
        r_up = np.concatenate([share * rp for share, _f, _s in trs])
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
        for z, (gbase, gcap, fz) in flex_info.items():
            fidx = np.asarray(fz["idx"], int); mu = fidx.size
            gc = gcap[fidx]                                 # (mu, n) available cap of the flex units
            ab = np.asarray(fz["alpha_band"], float); at = np.asarray(fz["alpha_tech"], float)
            dband = np.repeat(ab - at, n); ab_rep = np.repeat(ab, n)
            c_mod = float(fz["c_mod"]); c_start = np.asarray(fz["c_start"], float)
            ub = add_block(np.zeros(mu * n), np.zeros(mu * n), gc.ravel())                 # u ∈ [0, avail·cap]
            db = add_block(np.full(mu * n, c_mod), np.zeros(mu * n), np.full(mu * n, _INF))  # deep-mod d ≥ 0
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
    return {
        "n": n, "zones": zones, "ncol": ncol, "nrow": nrow,
        "col_cost": np.concatenate(col_cost), "col_lo": np.concatenate(col_lo), "col_up": np.concatenate(col_up),
        "row_lower": row_lower, "row_upper": row_upper, "coo": (R, C, V),
        "bal_dual_ix": bal_dual_ix, "flow_cols": flow_cols, "ecap_rows": ecap_rows, "T": T,
        "gen_cols": gen_cols, "res_cols": res_cols, "ens_cols": ens_cols, "dump_cols": dump_cols,
        "srmc_by_unit": srmc_by_unit, "res_schemes": res_schemes, "flex_cols": flex_cols,
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
    solves stay independent (cold, byte-identical); only the object-construction cost is amortised."""
    global _HIGHS
    if _HIGHS is None:
        _HIGHS = highspy.Highs()
        _HIGHS.setOptionValue("output_flag", False)
        _HIGHS.setOptionValue("presolve", "on")
    return _HIGHS


def _solve_and_read(h, spec, price_sign, diagnose: bool = False):
    if h.run() != highspy.HighsStatus.kOk or h.getModelStatus() != highspy.HighsModelStatus.kOptimal:
        raise RuntimeError(f"highs LP not optimal: {h.getModelStatus()}")
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
    if diagnose:                       # lecture seule de la solution primale (cf. lp.diagnostics)
        from .diagnostics import binding_flows, marginal_report
        out["diag"] = marginal_report(spec, cv, prices)
        out["diag_flows"] = binding_flows(spec, cv, {})
    return out


def solve_multizone_highs(times, zones_data: dict, borders: list, ntc: dict,
                          res_bid=-10.0, voll: float = 15000.0, price_floor=-500.0,
                          res_tranches: dict | None = None, price_sign: float = 1.0,
                          diagnose: bool = False, flex: dict | None = None) -> dict:
    """Cold-build + solve one window's dispatch LP directly in HiGHS. Same contract as
    ``multi_zone.solve_multizone`` (returns per-zone prices, flows, water values, objective).

    `price_sign` maps the HiGHS row dual to the market price; -1.0 reproduces linopy's sign (validated).
    `flex` opts into the plant-operating-rigidity rows (see ``_build``); None keeps the pure LP."""
    spec = _build(times, zones_data, borders, ntc, res_bid, voll, price_floor, res_tranches, flex)
    model = highspy.HighsModel()
    lp = model.lp_
    lp.num_col_ = spec["ncol"]; lp.num_row_ = spec["nrow"]
    lp.sense_ = highspy.ObjSense.kMinimize
    lp.col_cost_ = spec["col_cost"]; lp.col_lower_ = spec["col_lo"]; lp.col_upper_ = spec["col_up"]
    lp.row_lower_ = spec["row_lower"]; lp.row_upper_ = spec["row_upper"]
    indptr, indices, data = _to_csc(spec["ncol"], spec["nrow"], spec["coo"])
    lp.a_matrix_.format_ = highspy.MatrixFormat.kColwise
    lp.a_matrix_.start_ = indptr; lp.a_matrix_.index_ = indices; lp.a_matrix_.value_ = data
    h = _get_highs()
    h.passModel(model)
    return _solve_and_read(h, spec, price_sign, diagnose)
