"""FLEX-F6 — dual quality & diagnostics (spec §8): the SRMC tie-break bound + flag-off preservation, the
dual-oscillation detector, and the debug-dump price decomposition."""
from __future__ import annotations

import pandas as pd
from dispatch_model.lp.diagnostics import dual_oscillation
from dispatch_model.lp.highs_solver import _EPS_TIE, solve_multizone_highs


def _zone(demand, stack):
    T = pd.date_range("2024-05-19", periods=len(demand), freq="h", tz="UTC")
    zd = {"N": {"stack": stack, "demand": list(map(float, demand)), "res_pot": [0.0] * len(demand),
                "avail": None, "energy_caps": None}}
    return T, zd


_SISTERS = pd.DataFrame({"unit_id": ["A", "B", "C"], "tech": ["nuclear"] * 3, "capacity_mw": [100.0] * 3,
                         "srmc_eur_mwh": [10.0] * 3, "min_gen_frac": [0.0] * 3})


def test_tie_break_bounds_price_move_and_flag_off_is_untouched():
    T, zd = _zone([150.0] * 6, _SISTERS)
    # empty-idx flex → only the SRMC tie-break fires (no rigidity rows)
    flex = {"N": {"idx": [], "alpha_band": [], "alpha_tech": [], "c_mod": 8.0, "c_start": []}}
    on = solve_multizone_highs(T, zd, [], {}, flex=flex)["prices"]["N"]
    off = solve_multizone_highs(T, zd, [], {})["prices"]["N"]
    assert abs(off.iloc[0] - 10.0) < 1e-9                  # flag-off: unperturbed, exactly the shared SRMC
    assert (on - off).abs().max() <= _EPS_TIE + 1e-9       # tie-break never moves a price by more than ε


def test_dual_oscillation_flags_spurious_jump_not_a_real_move():
    idx = pd.date_range("2024-01-01", periods=4, freq="h", tz="UTC")
    prices = pd.DataFrame({"N": [10.0, 40.0, 41.0, 10.0]}, index=idx)
    demand = pd.DataFrame({"N": [1000.0, 1000.0, 1500.0, 1000.0]}, index=idx)
    ts = set(dual_oscillation(prices, demand, price_jump=20.0, demand_frac=0.02)["timestamp_utc"])
    assert idx[1] in ts                                    # 10→40 with flat demand → spurious, flagged
    assert idx[2] not in ts                                # 40→41 tiny → not flagged
    assert idx[3] not in ts                                # 41→10 but demand moved 1500→1000 → real move


def test_no_spurious_oscillation_on_a_degenerate_sister_fleet():
    # flat demand + identical sister units = the degenerate case; with the tie-break the price is stable.
    T, zd = _zone([150.0] * 12, _SISTERS)
    flex = {"N": {"idx": [], "alpha_band": [], "alpha_tech": [], "c_mod": 8.0, "c_start": []}}
    pr = solve_multizone_highs(T, zd, [], {}, flex=flex)["prices"]
    dem = pd.DataFrame({"N": [150.0] * 12}, index=pr.index)
    assert dual_oscillation(pr, dem, price_jump=0.5).empty   # no jump > 0.5 €/MWh on flat demand


def test_debug_hour_decomposes_a_nuclear_negative_print():
    stack = pd.DataFrame({"unit_id": ["NUC", "GAS"], "tech": ["nuclear", "gas"],
                          "capacity_mw": [40000.0, 10000.0], "srmc_eur_mwh": [7.0, 80.0],
                          "min_gen_frac": [0.0, 0.0]})
    T, zd = _zone([38000, 38000, 18000, 38000], stack)
    flex = {"N": {"idx": [0], "alpha_band": [0.60], "alpha_tech": [0.25], "c_mod": 8.0, "c_start": [2000.0]}}
    out = solve_multizone_highs(T, zd, [], {}, flex=flex, diagnose=True)
    dump = out["debug"]("N", 2)                            # the trough hour prints srmc − c_mod = −1
    assert dump["price"] < 0
    deep = [f for f in dump["flex_units"] if f["deepmod_mw"] > 1.0]
    assert deep, "the reactor should be deep-modulating at the trough"
    assert abs(deep[0]["implied_bid"] - dump["price"]) < 1.0    # implied bid srmc−c_mod = the balance price
    assert "C1a-band-floor" in deep[0]["flags"]
