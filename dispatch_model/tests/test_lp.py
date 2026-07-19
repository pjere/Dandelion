"""DISP Phase 5 — single-zone LP: prices are the balance duals across all regimes (synthetic, no DB)."""
from __future__ import annotations

import numpy as np
import pandas as pd
from dispatch_model.lp.single_zone import solve_window

_STACK = pd.DataFrame([
    ("NUC", 40000, 7.0, 0.30),      # must-run modulation floor
    ("GAS", 10000, 60.0, 0.0),
    ("OIL", 5000, 200.0, 0.0),
    ("DSR", 2000, 1000.0, 0.0),     # DSR tranche = high-SRMC "unit"
], columns=["unit_id", "capacity_mw", "srmc_eur_mwh", "min_gen_frac"])


def _run(scen, res_bid=-10.0, voll=15000.0, floor=-500.0):
    T = pd.date_range("2024-01-01", periods=len(scen), freq="h", tz="UTC")
    D = [s[0] for s in scen]; R = [s[1] for s in scen]
    return solve_window(T, D, R, _STACK, res_bid=res_bid, voll=voll, price_floor=floor), T, D


def test_prices_by_regime():
    scen = [(8000, 0), (15000, 6000), (25000, 3000), (48000, 0), (53000, 0), (56000, 0), (60000, 0)]
    out, T, D = _run(scen)
    p = out["price"].to_numpy()
    assert p[0] == -500                         # deep oversupply (nuclear floor > load) → price floor
    assert p[1] == -10                          # RES curtailment → res_bid (endogenous negative price)
    assert p[2] == 7                            # nuclear marginal
    assert p[3] == 60 and p[4] == 200           # gas, oil
    assert p[5] == 1000                         # DSR tranche
    assert p[6] == 15000                        # VoLL (unserved energy)


def test_balance_and_curtailment():
    scen = [(8000, 0), (15000, 6000), (25000, 3000), (60000, 0)]
    out, T, D = _run(scen)
    aux = out["aux"].set_index("time")
    g = out["dispatch"].groupby("time")["output_mw"].sum().reindex(T)
    residual = (g + aux["res_mw"] + aux["unserved_mw"] - aux["dump_mw"]).to_numpy() - np.array(D, float)
    assert np.abs(residual).max() < 1e-6                     # exact energy balance
    assert aux["curtailed_mw"].iloc[1] == 3000              # RES curtailed when floor + RES > load
    assert aux["unserved_mw"].iloc[3] > 0                   # unserved in the scarcity hour


def test_border_imports_cap_scarcity():
    # domestic supply = 57 GW; demand 60 GW hits VoLL without imports
    T = pd.date_range("2024-01-01", periods=1, freq="h", tz="UTC")
    assert solve_window(T, [60000], [0], _STACK)["price"].iloc[0] == 15000        # VoLL, autarky
    imp = solve_window(T, [60000], [0], _STACK, imports=[("IMP", 8000, 85.0)])
    assert imp["price"].iloc[0] < 15000                                          # scarcity removed
    assert imp["dispatch"].set_index("unit_id")["output_mw"]["IMP"] == 8000      # imports fully used
    assert imp["aux"]["unserved_mw"].iloc[0] < 1e-6


def test_border_imports_set_marginal_price():
    # demand 54 GW: nuclear+gas=50, imports supply the marginal 4 GW → price = import price
    T = pd.date_range("2024-01-01", periods=1, freq="h", tz="UTC")
    imp = solve_window(T, [54000], [0], _STACK, imports=[("IMP", 8000, 85.0)])
    assert imp["price"].iloc[0] == 85
    assert 0 < imp["dispatch"].set_index("unit_id")["output_mw"]["IMP"] < 8000   # partially loaded


def test_availability_scales_capacity():
    import xarray as xr
    T = pd.date_range("2024-01-01", periods=1, freq="h", tz="UTC")
    # knock nuclear to 50% available → less nuclear, more gas, price still gas=60 but ens if too tight
    av = xr.DataArray(np.array([[0.5], [1.0], [1.0], [1.0]]),
                      coords=[("unit", _STACK["unit_id"].to_numpy()), ("time", T)])
    out = solve_window(T, [48000], [0], _STACK, avail=av)
    g = out["dispatch"].set_index("unit_id")["output_mw"]
    assert g["NUC"] <= 20000 + 1e-6                          # capped at 50% of 40 GW
