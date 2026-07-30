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


def test_solar_uplift_recovers_a_curtailment_dip():
    # 20 identical sunny days, one day with a midday 40% curtailment dip: the envelope uplift must recover
    # ~the dip on that day and stay ≈0 elsewhere (the price-based noise floor only trims the estimator).
    from dispatch_model.flexibility.res_potential import solar_uplift
    idx = pd.date_range("2024-05-01", periods=20 * 24, freq="h", tz="UTC")
    shape = np.array([0, 0, 0, 0, 0, 1, 3, 6, 8, 9, 10, 10, 10, 9, 8, 6, 3, 1, 0, 0, 0, 0, 0, 0]) * 1000.0
    gen = np.tile(shape, 20)
    day = 10
    gen[day * 24 + 10: day * 24 + 14] *= 0.6                    # the curtailed midday
    g = pd.DataFrame({"timestamp_utc": idx, "tech": "solar", "gen_mw": gen})
    prices = pd.Series(50.0, index=idx)                         # all uncensored → noise floor from all hours
    up = solar_uplift(g, prices)
    assert up.loc[idx[day * 24 + 12]] > 3500                    # ~4 GW recovered at the dip
    assert up.drop(idx[day * 24 + 10: day * 24 + 14]).max() < 500   # ≈0 away from the dip


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


def test_btm_solar_reconstructs_invisible_fleet_from_metered_shape():
    """NL salderen fix: the metered utility sliver carries the irradiance shape; the invisible
    capacity (installed - utility p99.9) rides it at the rooftop derate. Empty when nothing is
    invisible or the metered fleet is too small to carry a shape."""
    import numpy as np
    import pandas as pd
    import pytest
    from dispatch_model.flexibility.res_potential import _BTM_DERATE, _BTM_NETTED, btm_solar

    idx = pd.date_range("2025-06-01", periods=21 * 24, freq="h", tz="UTC")
    shape = np.clip(np.sin((idx.hour - 6) / 12 * np.pi), 0, None)          # daylight bell, peak 1.0
    gen = pd.DataFrame({"timestamp_utc": idx, "tech": "solar", "gen_mw": 400.0 * shape})
    out = btm_solar(gen, installed_solar_mw=29_000.0)
    assert not out.empty
    peak = out[idx.hour == 12].mean()                                       # noon rides ~full shape
    assert peak == pytest.approx((29_000.0 - 400.0) * _BTM_DERATE * (1 - _BTM_NETTED), rel=0.05)
    assert float(out[idx.hour == 0].max()) == 0.0                           # dark hours contribute 0
    assert btm_solar(gen, installed_solar_mw=300.0).empty                   # nothing invisible
    tiny = gen.assign(gen_mw=gen["gen_mw"] * 0.05)                          # 20 MW fleet: no shape
    assert btm_solar(tiny, installed_solar_mw=29_000.0).empty
