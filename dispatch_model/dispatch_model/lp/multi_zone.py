"""Multi-zone economic dispatch LP (NTC-coupled) — the 7-zone price-formation capstone.

Extends the single-zone formulation to N zonal energy balances linked by NTC-bounded cross-border flows.
Each zone's price is the dual of its balance; spreads (FR–DE, …) form endogenously from relative stacks
and binding NTCs.

  min Σ_z Σ_t [ Σ_u srmc·gen + res_bid·res_z + VoLL·ens_z + dump_cost·dump_z ] + ε Σ_k (fwd_k + bwd_k)
  s.t. per zone z, hour t:
        Σ_{u∈z} gen + res_z + ens_z − dump_z + imports_z = demand_z         (balance_{z,t}; dual = price)
        where imports_z = Σ_{(a,z)} (fwd−bwd) − Σ_{(z,b)} (fwd−bwd)
        0 ≤ fwd_k ≤ NTC_ab ,  0 ≤ bwd_k ≤ NTC_ba          (directed flows, k=(a,b))
        gmin ≤ gen ≤ gcap · availability ; 0 ≤ res ≤ res_pot ; ens,dump ≥ 0

Directed non-negative flows + a tiny gross-flow cost ε remove degenerate loop flows. GB (no ENTSO-E data)
is represented upstream as border supply tranches on the FR/BE stacks, not as a balance here.
"""
from __future__ import annotations

import linopy
import numpy as np
import pandas as pd
import xarray as xr

_EPS_FLOW = 1e-3          # €/MWh gross-flow penalty → removes loop flows, keeps duals clean


def _as_time_array(v, n: int) -> np.ndarray:
    a = np.asarray(v, float)
    return np.full(n, float(v)) if a.ndim == 0 else a


def solve_multizone(times, zones_data: dict, borders: list, ntc: dict,
                    res_bid: float | dict = -10.0, voll: float = 15000.0,
                    price_floor: float | dict = -500.0, solver: str = "highs",
                    res_tranches: dict | None = None) -> dict:
    """Solve the coupled dispatch over one window.

    zones_data[zone] = {"stack": df(unit_id,tech,capacity_mw,srmc_eur_mwh,min_gen_frac),
                        "demand": array, "res_pot": array, "avail": DataArray|None,
                        "energy_caps": {tech: MWh}|None}
    borders = list of (a,b); ntc[(a,b)] = (ntc_ab, ntc_ba) scalars or per-hour arrays.

    `res_bid` / `price_floor` accept a scalar (applied to every zone) or a {zone: value} mapping.
    Per-zone values are required because market *rules* differ: negative prices were prohibited in
    IT-North until the TIDE reform (Jan-2025) and in ES until Dec-2023, so those zone-years are
    floored at 0 — a regulatory fact, not a fitted parameter (and the floor is gone for both in the
    2027-46 projection). Per-zone bids are also the hook for the RES subsidy tranches.

    Returns per-zone prices, dispatch, flows, and water values.
    """
    T = pd.DatetimeIndex(times)
    zones = list(zones_data)
    m = linopy.Model()

    # ---- per-zone generation variables + balance pieces ----
    gen_vars, srmc_das, unit_zone, cap_cons = {}, {}, {}, {}
    for z in zones:
        st = zones_data[z]["stack"]
        U = pd.Index(st["unit_id"].to_numpy(), name="unit")
        cap = xr.DataArray(st["capacity_mw"].to_numpy(float), coords=[U])
        minf = xr.DataArray(st["min_gen_frac"].to_numpy(float), coords=[U])
        av = zones_data[z].get("avail")
        if av is None:
            av = xr.DataArray(np.ones((len(U), len(T))), coords=[U, ("time", T)])
        else:
            av = av.reindex(unit=U, time=T).fillna(0.0)
        gcap = (av * cap).transpose("unit", "time")
        g = m.add_variables(lower=gcap * minf, upper=gcap, name=f"gen_{z}")
        gen_vars[z] = g
        srmc_das[z] = xr.DataArray(st["srmc_eur_mwh"].to_numpy(float), coords=[U])
        unit_zone[z] = st

    # ---- RES must-take: either one block per zone (flat res_bid) or a scheme-tranche supply curve ----
    # Tranched mode gives each subsidy scheme its own curtailment floor = −(premium it keeps at negative
    # prices), so RES has a *supply curve* at negative prices (reality's mean ≈ −17, not a flat −10) and
    # the §51 EEG trigger can zero a tranche's premium after N consecutive negative hours (path-dependent;
    # the caller runs the fixed point and passes time-varying floors here). See RES_BIDDING_DESIGN.md §2.
    res_terms, res_obj = {}, 0.0
    if res_tranches is None:
        res = m.add_variables(lower=0, upper=xr.concat(
            [xr.DataArray(np.clip(zones_data[z]["res_pot"], 0, None), coords=[("time", T)]).expand_dims(zone=[z])
             for z in zones], "zone"), name="res")
        res_bid_da0 = xr.DataArray([float(res_bid) if np.isscalar(res_bid) else float(res_bid[z]) for z in zones],
                                   coords=[("zone", zones)])
        res_obj = (res * res_bid_da0).sum()
        res_terms = {z: res.sel(zone=z) for z in zones}
    else:
        for z in zones:
            trs = res_tranches[z]
            Tr = pd.Index([t["scheme"] for t in trs], name="res_tranche")
            rp = np.clip(np.asarray(zones_data[z]["res_pot"], float), 0, None)
            upper = xr.DataArray(np.array([t["share"] * rp for t in trs]), coords=[Tr, ("time", T)])
            v = m.add_variables(lower=0, upper=upper, name=f"res_{z}")
            floor = xr.DataArray(np.array([_as_time_array(t["floor"], len(T)) for t in trs]),
                                 coords=[Tr, ("time", T)])
            res_obj = res_obj + (v * floor).sum()
            res_terms[z] = v.sum("res_tranche")
    ens = m.add_variables(lower=0, coords=[("zone", zones), ("time", T)], name="ens")
    dump = m.add_variables(lower=0, coords=[("zone", zones), ("time", T)], name="dump")

    # ---- directed flows per border (skipped entirely for a single isolated zone) ----
    bnames = [f"{a}>{b}" for a, b in borders]
    fwd = bwd = None
    if borders:
        def _ntc(i, d):
            v = ntc[borders[i]][d]
            return xr.DataArray(np.full(len(T), v) if np.isscalar(v) else np.asarray(v), coords=[("time", T)])
        fwd_up = xr.concat([_ntc(i, 0).expand_dims(border=[bnames[i]]) for i in range(len(borders))], "border")
        bwd_up = xr.concat([_ntc(i, 1).expand_dims(border=[bnames[i]]) for i in range(len(borders))], "border")
        fwd = m.add_variables(lower=0, upper=fwd_up, name="fwd")     # a→b
        bwd = m.add_variables(lower=0, upper=bwd_up, name="bwd")     # b→a

    # ---- zonal balances ----
    balances = {}
    for z in zones:
        gz = gen_vars[z].sum("unit")
        imp = None
        for (a, b), nm in zip(borders, bnames):
            if z == b:                                          # a→z imports, z→a exports
                term = fwd.sel(border=nm) - bwd.sel(border=nm)
            elif z == a:
                term = bwd.sel(border=nm) - fwd.sel(border=nm)
            else:
                continue
            imp = term if imp is None else imp + term
        lhs = gz + res_terms[z] + ens.sel(zone=z) - dump.sel(zone=z)
        if imp is not None:
            lhs = lhs + imp
        D = xr.DataArray(np.asarray(zones_data[z]["demand"], float), coords=[("time", T)])
        balances[z] = m.add_constraints(lhs == D, name=f"bal_{z}")
        # per-zone energy caps (hydro budgets)
        for tech, mwh in (zones_data[z].get("energy_caps") or {}).items():
            uids = unit_zone[z].loc[unit_zone[z]["tech"] == tech, "unit_id"].to_numpy()
            if len(uids):
                cap_cons[(z, tech)] = m.add_constraints(
                    gen_vars[z].sel(unit=uids).sum() <= float(mwh), name=f"ecap_{z}_{tech}")

    def _per_zone(v):
        """scalar → same value for every zone; {zone: value} → aligned DataArray."""
        vals = [float(v) for _ in zones] if np.isscalar(v) else [float(v[z]) for z in zones]
        return xr.DataArray(vals, coords=[("zone", zones)])

    floor_da = _per_zone(price_floor)
    obj = sum((gen_vars[z] * srmc_das[z]).sum() for z in zones) \
        + res_obj + ens.sum() * voll + (dump * (-floor_da)).sum()
    if fwd is not None:
        obj = obj + (fwd.sum() + bwd.sum()) * _EPS_FLOW
    m.add_objective(obj)
    m.solve(solver_name=solver)
    if m.status != "ok":
        raise RuntimeError(f"multizone LP not solved: {m.status}/{m.termination_condition}")

    prices = pd.DataFrame({z: balances[z].dual.to_pandas().reindex(T) for z in zones})
    if fwd is not None:
        net = (fwd.solution - bwd.solution).to_pandas()         # border × time, a→b net
        flows = net.T.reset_index().melt(id_vars="time", var_name="border", value_name="flow_mw")
    else:
        flows = pd.DataFrame(columns=["time", "border", "flow_mw"])
    water = {f"{z}:{t}": float(-c.dual) for (z, t), c in cap_cons.items()}
    return {"prices": prices, "flows": flows, "water_values": water, "objective": float(m.objective.value)}
