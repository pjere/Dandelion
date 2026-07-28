"""Neighbour-zone FLEX — pseudo-unit split + spec builder + the block-level negative-formation mechanism."""
from __future__ import annotations

import numpy as np
import pandas as pd
from dispatch_model.flexibility import neighbour_nuclear as nn
from dispatch_model.lp.highs_solver import solve_multizone_highs

_BE = pd.DataFrame({"unit_id": ["BE_nuclear", "BE_gas_0", "BE_gas_1"],
                    "tech": ["nuclear", "gas", "gas"],
                    "capacity_mw": [4000.0, 2000.0, 2000.0],
                    "srmc_eur_mwh": [9.0, 70.0, 85.0], "min_gen_frac": [0.5, 0.0, 0.0]})
_COSTS = {"c_start_900": 300.0, "c_start_1300": 320.0}


def test_split_makes_reactor_scale_pseudo_units_and_keeps_the_rest():
    st = nn.split_nuclear_block(_BE, "BE")
    nuc = st[st["tech"] == "nuclear"]
    assert len(nuc) == 4 and np.allclose(nuc["capacity_mw"], 1000.0)     # 4 GW → 4×1 GW slices
    assert float(nuc["capacity_mw"].sum()) == 4000.0
    assert (st["tech"] == "gas").sum() == 2                              # non-nuclear rows untouched
    assert nn.split_nuclear_block(_BE[_BE["tech"] == "gas"], "BE").equals(
        _BE[_BE["tech"] == "gas"].reset_index(drop=True))                # no nuclear → unchanged


def test_spec_uses_zone_anchors_and_the_beta_ceiling():
    st = nn.split_nuclear_block(_BE, "BE")
    spec = nn.build_neighbour_flex_spec(st, "BE", _COSTS)
    a = nn._ANCHORS["BE"]
    assert list(spec["idx"]) == list(np.flatnonzero(st["tech"] == "nuclear"))
    assert (spec["alpha_band"] >= a["alpha_op"] - 1e-9).all()            # fleet-operating floor
    from dispatch_model.stacks.costs import nuclear_srmc
    assert abs(spec["c_mod"] - (nuclear_srmc() - a["socle_bid"])) < 1e-6  # srmc proxy − socle bid
    ceil = spec["r_up"] / (8 * (spec["alpha_band"] - spec["alpha_tech"]))
    assert (spec["xenon_beta"] <= ceil + 1e-9).all()                     # no xénon death spiral
    assert (spec["u_min_frac"] == a["kappa"]).all()
    assert nn.build_neighbour_flex_spec(_BE[_BE["tech"] == "gas"], "BE", _COSTS) is None


def test_block_level_fleet_prints_a_negative_in_surplus():
    # a BE-like zone alone in deep surplus: the committed pseudo-fleet's operating floor exceeds demand,
    # the RES tranche curtails at its floor → the balance dual goes negative (the F7 mechanism, block-level).
    st = nn.split_nuclear_block(_BE, "BE").reset_index(drop=True)
    st["min_gen_frac"] = 0.0
    spec = nn.build_neighbour_flex_spec(st, "BE", _COSTS)
    T = pd.date_range("2024-04-06", periods=6, freq="h", tz="UTC")
    dem = [3800.0, 3800.0, 1200.0, 1200.0, 1200.0, 3800.0]
    zd = {"BE": {"stack": st, "demand": dem, "res_pot": [500.0] * 6, "avail": None, "energy_caps": None}}
    out = solve_multizone_highs(T, zd, [], {}, res_bid=-8.0, price_floor=-500.0, flex={"BE": spec})
    pr = out["prices"]["BE"]
    # committed floor ≈ κ·α_op·4 GW ≈ 2.66 GW > trough net load 0.7 GW → forced surplus → negative print
    assert pr.iloc[3] < 0
