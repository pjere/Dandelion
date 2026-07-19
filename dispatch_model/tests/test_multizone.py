"""DISP Phase 5b — multi-zone NTC-coupled LP: endogenous spreads + convergence (synthetic, no DB)."""
from __future__ import annotations

import pandas as pd
from dispatch_model.lp.multi_zone import solve_multizone

_A = pd.DataFrame([("A_NUC", "nuclear", 40000, 7.0, 0.0), ("A_GAS", "gas", 10000, 40.0, 0.0)],
                  columns=["unit_id", "tech", "capacity_mw", "srmc_eur_mwh", "min_gen_frac"])
_B = pd.DataFrame([("B_GAS", "gas", 20000, 80.0, 0.0)],
                  columns=["unit_id", "tech", "capacity_mw", "srmc_eur_mwh", "min_gen_frac"])


def _run(ntc, dB=24000):
    T = pd.date_range("2024-01-01", periods=1, freq="h", tz="UTC")
    zd = {"A": {"stack": _A, "demand": [30000], "res_pot": [0]},
          "B": {"stack": _B, "demand": [dB], "res_pot": [0]}}
    out = solve_multizone(T, zd, [("A", "B")], {("A", "B"): (ntc, ntc)})
    return out["prices"].iloc[0], out["flows"]["flow_mw"].iloc[0]


def test_ntc_binding_creates_spread():
    pr, flow = _run(5000)
    assert pr["A"] == 7 and pr["B"] == 80          # cheap A (nuclear), expensive B (gas), decoupled
    assert flow == 5000                             # flow pinned at NTC (cheap → expensive)


def test_ample_ntc_converges_prices():
    pr, flow = _run(25000)
    assert abs(pr["A"] - pr["B"]) < 0.1             # converge to one marginal (± ε loop-flow penalty)
    assert abs(pr["A"] - 80) < 0.1                  # the common system marginal (B gas)
    assert 0 < flow < 25000                         # flow below NTC → not binding


def test_flow_direction_cheap_to_expensive():
    pr, flow = _run(3000)
    assert flow > 0                                 # A (cheap) exports to B (expensive)
    assert pr["B"] > pr["A"]
