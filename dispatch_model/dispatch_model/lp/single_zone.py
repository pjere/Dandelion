"""Single-zone economic dispatch LP (degraded mode / FR-only) — the price-formation core.

Least-cost dispatch of a unit stack against net load over a time window, with scarcity priced INSIDE the
LP so prices come out as duals (never post-processed):

  min  Σ_t [ Σ_u srmc_u · gen_{u,t} + res_bid · res_t + VoLL · ens_t + dump_cost · dump_t ]
  s.t. Σ_u gen_{u,t} + res_t + ens_t − dump_t = demand_t        (balance_t ; dual = price_t)
       gmin_{u,t} ≤ gen_{u,t} ≤ gcap_{u,t}                       (nuclear modulation floor, availability)
       0 ≤ res_t ≤ res_pot_t                                     (RES injected; curtailment = pot − res)
       ens_t ≥ 0 (unserved @ VoLL) ,  dump_t ≥ 0 (over-gen @ price floor)

Endogenous prices: marginal thermal SRMC in normal hours; **negative** when a must-run nuclear floor +
subsidised RES (res_bid < 0) exceed load; **VoLL** when load is unserved. DSR tranches enter simply as
high-SRMC "units" in the stack, so they step the scarcity price below VoLL with no special casing.
"""
from __future__ import annotations

import linopy
import numpy as np
import pandas as pd
import xarray as xr


def border_tranches(tranches) -> pd.DataFrame:
    """Represent a degraded-mode border supply curve as high-SRMC pseudo-units (imports).

    `tranches` = list of (name, capacity_mw, price_eur_mwh). They enter the stack like any unit, so
    imports cap the price in tight hours instead of jumping to VoLL — the spec's 1-zone-mode remedy.
    """
    rows = [{"unit_id": nm, "name": nm, "tech": "import", "capacity_mw": float(cap),
             "srmc_eur_mwh": float(pr), "min_gen_frac": 0.0, "efficiency": float("nan"),
             "ramp_frac": 1.0, "vom": 0.0} for nm, cap, pr in tranches]
    return pd.DataFrame(rows)


def solve_window(times, demand, res_pot, stack: pd.DataFrame, avail: xr.DataArray | None = None,
                 res_bid: float = -10.0, voll: float = 15000.0, price_floor: float = -500.0,
                 imports=None, energy_caps: dict | None = None, solver: str = "highs") -> dict:
    """Solve one window. `stack` needs columns unit_id, capacity_mw, srmc_eur_mwh, min_gen_frac.

    `avail` (unit×time, 0..1) scales capacity; default fully available. `imports` = optional list of
    (name, capacity_mw, price) border tranches (single-zone mode). `energy_caps` = {tech: max_MWh over
    the window} (e.g. the weekly reservoir budget) — the LP self-allocates that energy to peak hours and
    the tech's **water value** is returned as the cap's dual. Returns prices, dispatch, aux, water_values.
    """
    if imports:
        stack = pd.concat([stack, border_tranches(imports)], ignore_index=True)
    T = pd.DatetimeIndex(times)
    U = stack["unit_id"].to_numpy()
    cap = xr.DataArray(stack["capacity_mw"].to_numpy(float), coords=[("unit", U)])
    srmc = xr.DataArray(stack["srmc_eur_mwh"].to_numpy(float), coords=[("unit", U)])
    minf = xr.DataArray(stack["min_gen_frac"].to_numpy(float), coords=[("unit", U)])
    if avail is None:
        avail = xr.DataArray(np.ones((len(U), len(T))), coords=[("unit", U), ("time", T)])
    else:
        avail = avail.reindex(unit=U, time=T).fillna(0.0)
    gcap = (avail * cap).transpose("unit", "time")
    gmin = gcap * minf                                             # floor only where available

    D = xr.DataArray(np.asarray(demand, float), coords=[("time", T)])
    R = xr.DataArray(np.clip(np.asarray(res_pot, float), 0, None), coords=[("time", T)])

    m = linopy.Model()
    gen = m.add_variables(lower=gmin, upper=gcap, name="gen")
    res = m.add_variables(lower=0, upper=R, name="res")
    ens = m.add_variables(lower=0, coords=[("time", T)], name="ens")
    dump = m.add_variables(lower=0, coords=[("time", T)], name="dump")

    balance = m.add_constraints(gen.sum("unit") + res + ens - dump == D, name="balance")

    # per-tech energy budgets over the window (e.g. weekly reservoir budget) → dual = water value
    cap_cons = {}
    if energy_caps and "tech" in stack.columns:
        techs = stack["tech"].to_numpy()
        for tech, mwh in energy_caps.items():
            uids = stack.loc[techs == tech, "unit_id"].to_numpy()
            if len(uids):
                cap_cons[tech] = m.add_constraints(gen.sel(unit=uids).sum() <= float(mwh),
                                                   name=f"ecap_{tech}")

    m.add_objective((gen * srmc).sum() + (res * res_bid).sum()
                    + ens.sum() * voll + dump.sum() * (-price_floor))
    m.solve(solver_name=solver)
    if m.status != "ok":
        raise RuntimeError(f"LP not solved: status={m.status} termination={m.termination_condition}")

    price = balance.dual.to_pandas().reindex(T)
    water_values = {t: float(-c.dual) for t, c in cap_cons.items()}      # marginal value of + 1 MWh budget
    gen_sol = gen.solution.to_pandas()                            # unit × time
    disp = (gen_sol.T.reset_index().melt(id_vars="time", var_name="unit_id", value_name="output_mw"))
    aux = pd.DataFrame({"time": T, "res_mw": res.solution.to_pandas().reindex(T).to_numpy(),
                        "res_pot_mw": R.to_pandas().to_numpy(),
                        "unserved_mw": ens.solution.to_pandas().reindex(T).to_numpy(),
                        "dump_mw": dump.solution.to_pandas().reindex(T).to_numpy()})
    aux["curtailed_mw"] = (aux["res_pot_mw"] - aux["res_mw"]).clip(lower=0)
    return {"price": price.rename("price_eur_mwh"), "dispatch": disp, "aux": aux,
            "water_values": water_values, "objective": float(m.objective.value)}
