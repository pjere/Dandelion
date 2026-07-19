"""RES subsidy bid stack + §51 trigger (step vii negative-price mechanism)."""
from __future__ import annotations

import numpy as np
import pandas as pd
from dispatch_model.config import load_config
from dispatch_model.res_schemes import (
    _neg_runlength,
    _zone_tranches,
    load_res_schemes,
    solve_with_triggers,
)


def _wb():
    cfg = load_config("config.yaml")
    return cfg.resolve(cfg.section("assumptions")["workbook"])


# --- data model -------------------------------------------------------------
def test_schemes_load_and_shares_normalise():
    s = load_res_schemes(_wb())
    assert "DE_LU" in s and "BE" in s
    for zone, trs in s.items():
        assert abs(sum(t["share"] for t in trs) - 1.0) < 1e-6, zone
    de = {t["scheme"]: t for t in s["DE_LU"]}
    assert de["market_premium"]["floor"] < 0 and de["market_premium"]["trigger"] == 6  # §51 6h (2019)
    assert de["merchant"]["floor"] == 0.0 and de["merchant"]["trigger"] == 0
    be = {t["scheme"]: t for t in s["BE"]}
    assert be["green_certificate"]["trigger"] == 0                 # GCs paid regardless → no §51


# --- §51 run-length trigger -------------------------------------------------
def test_neg_runlength_counts_consecutive_and_resets():
    p = np.array([5, -1, -2, -3, 4, -1, -2])
    assert list(_neg_runlength(p)) == [0, 1, 2, 3, 0, 1, 2]


def test_floored_zone_gets_single_zero_tranche():
    tr = _zone_tranches("IT_NORTH", {}, res_bid_z=0.0, n=24)      # pre-TIDE regulatory floor
    assert len(tr) == 1 and tr[0]["scheme"] == "floored"
    assert np.all(tr[0]["floor"] == 0.0)


def test_normal_zone_uses_scheme_tranches():
    schemes = {"DE_LU": [{"scheme": "mp", "share": 1.0, "floor": -20.0, "trigger": 6}]}
    tr = _zone_tranches("DE_LU", schemes, res_bid_z=-10.0, n=10)
    assert tr[0]["scheme"] == "mp" and np.all(tr[0]["floor"] == -20.0)


# --- end-to-end: supply curve + trigger in a tiny 1-zone LP -----------------
def _one_zone(res_pot, demand, stack_srmc=40.0):
    T = pd.date_range("2019-06-01", periods=len(res_pot), freq="h", tz="UTC")
    stack = pd.DataFrame([{"unit_id": "G1", "tech": "gas", "capacity_mw": 5000.0,
                           "srmc_eur_mwh": stack_srmc, "min_gen_frac": 0.0}])
    zd = {"Z": {"stack": stack, "demand": np.asarray(demand, float),
                "res_pot": np.asarray(res_pot, float), "avail": None, "energy_caps": None}}
    return T, zd


def test_supply_curve_sets_negative_price_at_tranche_floor():
    # huge RES surplus, low demand → RES is marginal → price sits at the marginal tranche floor, not −10
    T, zd = _one_zone(res_pot=[8000] * 6, demand=[1000] * 6)
    schemes = {"Z": [{"scheme": "fit", "share": 0.5, "floor": -60.0, "trigger": 0},
                     {"scheme": "merchant", "share": 0.5, "floor": 0.0, "trigger": 0}]}
    out = solve_with_triggers(T, zd, [], {}, schemes, res_bid=-10.0, price_floor=-500.0)
    p = out["prices"]["Z"].to_numpy()
    assert np.all(p < 0)                                            # surplus ⇒ negative
    assert p.min() >= -60.5                                         # never below the deepest tranche floor
    assert p.max() <= 0.5


def test_trigger_lifts_floor_after_consecutive_negative_hours():
    # persistent surplus for many hours; a 3h-trigger tranche should stop bidding negative past hour 3
    T, zd = _one_zone(res_pot=[8000] * 8, demand=[1000] * 8)
    schemes = {"Z": [{"scheme": "mp", "share": 1.0, "floor": -30.0, "trigger": 3}]}
    out = solve_with_triggers(T, zd, [], {}, schemes, res_bid=-10.0, price_floor=-500.0)
    p = out["prices"]["Z"].to_numpy()
    assert p[0] < -1 and p[1] < -1                                 # early hours still negative (premium on)
    assert p[-1] >= -0.5                                           # after the trigger, floor lifted to ~0
