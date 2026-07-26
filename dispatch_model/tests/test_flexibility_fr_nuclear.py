"""FLEX-F2b — per-reactor FR nuclear stack + flex-spec builder (``flexibility.fr_nuclear``). Synthetic,
no DB: assert the spec shape, the fuel-cost-floored revealed-curve bids, and the maneuverability derate."""
from __future__ import annotations

import numpy as np
import pandas as pd

from dispatch_model.flexibility import fr_nuclear as fn
from dispatch_model.stacks.revealed import BID_COL, SupplyCurve

_STACK = pd.DataFrame({"unit_id": ["N900", "N1300", "N1450", "GAS"],
                       "tech": ["nuclear", "nuclear", "nuclear", "gas"],
                       "capacity_mw": [900.0, 1300.0, 1450.0, 500.0],
                       "srmc_eur_mwh": [7.0, 7.0, 7.0, 80.0], "min_gen_frac": [0.25, 0.25, 0.25, 0.0]})
# revealed curve with a deep sub-zero socle (74 % at −40) and a positive flexible top
_CURVE = SupplyCurve("FR", ((0.74, -40.0), (0.09, 0.0), (0.05, 10.0), (0.04, 30.0), (0.03, 60.0),
                            (0.05, 80.0)), tech="nuclear")
_COSTS = {"c_mod": 8.0, "c_start_900": 300.0, "c_start_1300": 320.0, "c_start_N4": 340.0}


def test_spec_indexes_only_the_nuclear_rows_with_class_physics():
    st, spec = fn.build_flex_spec(_STACK, _CURVE, c_mod=8.0, c_start_by_class=_COSTS)
    assert list(spec["idx"]) == [0, 1, 2]                       # the three nuclear rows, not the gas row
    assert spec["c_mod"] == 8.0
    assert list(spec["c_start"]) == [300.0, 320.0, 340.0]       # 900 / 1300 / N4(1450) class start costs
    for key in ("alpha_band", "alpha_tech", "r_up", "xenon_beta", "d_max_8h", "d_max_day", "rho_recommit"):
        assert np.asarray(spec[key]).shape == (3,)


def test_bids_come_from_the_revealed_curve_but_are_floored_at_the_fuel_cost():
    st, spec = fn.build_flex_spec(_STACK, _CURVE, c_mod=8.0, c_start_by_class=_COSTS, floor_bid=7.0)
    bids = st.loc[spec["idx"], BID_COL].to_numpy(float)
    assert (bids >= 7.0 - 1e-9).all()                          # the −40 socle bid is NOT written in
    assert bids.max() > 7.0                                    # the flexible top keeps the curve's slope
    assert st.loc[3, BID_COL] != st.loc[3, BID_COL]            # gas row untouched (NaN)


def test_no_curve_falls_back_to_a_uniform_fuel_cost_bid():
    st, spec = fn.build_flex_spec(_STACK, None, c_mod=8.0, c_start_by_class=_COSTS, floor_bid=7.0)
    bids = st.loc[spec["idx"], BID_COL].to_numpy(float)
    assert np.allclose(bids, 7.0)                              # future year / no observation → fuel cost


def test_maneuverability_derates_the_deepmod_caps():
    _, base = fn.build_flex_spec(_STACK, _CURVE, c_mod=8.0, c_start_by_class=_COSTS)
    mvr = {"N1300": ("reduced", 1.0), "N1450": ("none", 0.9)}
    _, spec = fn.build_flex_spec(_STACK, _CURVE, c_mod=8.0, c_start_by_class=_COSTS, maneuver=mvr)
    assert spec["d_max_day"][0] == base["d_max_day"][0]        # N900 full: unchanged
    assert spec["d_max_day"][1] == 0.5 * base["d_max_day"][1]  # reduced: half caps
    assert spec["d_max_day"][2] == 0.0                         # none (stretch-out): no deep-mod headroom


def test_no_nuclear_rows_returns_the_stack_unchanged():
    gas_only = _STACK[_STACK["tech"] == "gas"].reset_index(drop=True)
    st, spec = fn.build_flex_spec(gas_only, _CURVE, c_mod=8.0, c_start_by_class=_COSTS)
    assert spec is None
    assert st is gas_only


# ---- FLEX-F3: maneuverability keyed by plant name, fossil §4, reserves/minstab -------------------------
_NAMED = pd.DataFrame({"unit_id": ["17W1", "17W2"], "name": ["BLAYAIS 1", "BLAYAIS 2"],
                       "tech": ["nuclear", "nuclear"], "capacity_mw": [900.0, 1300.0],
                       "srmc_eur_mwh": [7.0, 7.0], "min_gen_frac": [0.25, 0.25], "ramp_frac": [0.05, 0.05]})
_FOSSIL = pd.DataFrame({"unit_id": ["G", "C", "H"], "name": ["g", "c", "h"],
                        "tech": ["gas", "coal", "hydro_reservoir"], "capacity_mw": [500.0, 600.0, 2000.0],
                        "srmc_eur_mwh": [80.0, 70.0, 5.0], "min_gen_frac": [0.0, 0.0, 0.0],
                        "ramp_frac": [1.0, 0.4, 1.0]})


def test_maneuverability_is_matched_by_plant_name():
    st, spec = fn.build_flex_spec(_NAMED, _CURVE, c_mod=8.0, c_start_by_class=_COSTS,
                                  maneuver={"BLAYAIS 2": ("none", 0.9)})
    assert spec["deepband_scale"][1] == 0.0 and spec["must_run_frac"][1] == 0.9   # matched on name, not EIC
    assert spec["deepband_scale"][0] == 1.0 and spec["must_run_frac"][0] == 0.0


def test_window_spec_rederates_only_the_named_week_states():
    st, base = fn.build_flex_spec(_NAMED, _CURVE, c_mod=8.0, c_start_by_class=_COSTS)
    w = fn.window_spec(base, {"BLAYAIS 1": ("reduced", 1.0)})
    assert w["deepband_scale"][0] == 0.5 and base["deepband_scale"][0] == 1.0    # base untouched (copy)
    assert w["d_max_8h"][0] == 0.5 * base["_d8_full"][0]
    assert fn.window_spec(base, None) is base                                    # no states → same object


def test_tail_state_extracts_reversed_deepmod_history():
    fz_out = {"u": np.array([[1.0, 2.0, 3.0, 4.0]]), "p": np.array([[10.0, 20.0, 30.0, 40.0]]),
              "d": np.array([[0.0, 5.0, 0.0, 7.0]])}
    st = fn.tail_state(fz_out)
    assert st["u_init"][0] == 4.0 and st["p_init"][0] == 40.0          # last-hour commit / output
    assert st["d_hist"].shape == (1, 8)                                # padded to the 8-hour lookback
    assert list(st["d_hist"][0][:3]) == [7.0, 0.0, 5.0]               # reversed: d_{-1}, d_{-2}, d_{-3}


def test_fossil_section_adds_min_load_and_reserve_idx():
    stack = pd.concat([_NAMED, _FOSSIL], ignore_index=True)
    st, spec = fn.build_flex_spec(stack, _CURVE, c_mod=8.0, c_start_by_class=_COSTS,
                                  r_up_req=1500.0, r_down_req=1000.0, p_minstab=45000.0,
                                  include_fossil=True, fossil_c_start={"c_start_gas": 30.0, "c_start_coal": 80.0})
    assert list(spec["is_nuclear"]) == [True, True, False, False]               # 2 nuclear + gas + coal
    assert spec["alpha_band"][2] == fn._FOSSIL_MIN_LOAD["gas"]                   # gas min stable load
    assert spec["alpha_band"][2] == spec["alpha_tech"][2]                       # no deep band for fossil
    assert spec["r_up"][3] == 0.4                                               # coal ramp fraction (not 0)
    assert list(spec["reserve_idx"]) == [4]                                     # the hydro_reservoir row
    assert spec["r_up_req"] == 1500.0 and spec["p_minstab"] == 45000.0
