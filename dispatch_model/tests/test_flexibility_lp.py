"""FLEX-F2 — nuclear rigidity core in the LP (C1 two-tier min + C2 c_mod + C5 start cost). Synthetic,
no DB: assert the balance duals go negative exactly when a committed reactor is forced into surplus."""
from __future__ import annotations

import pandas as pd
from dispatch_model.lp.highs_solver import solve_multizone_highs

# 1 zone: a big cheap nuclear unit (flex) + expensive gas. min_gen_frac 0 → the flex C1 band governs the
# floor. Demand is high, high, deep trough, high — the recovery in the last hour is what makes C5's start
# cost hold `u` through the trough (without a recovery the LP just drops commitment for free).
_STACK = pd.DataFrame({"unit_id": ["NUC", "GAS"], "tech": ["nuclear", "gas"],
                       "capacity_mw": [40000.0, 10000.0], "srmc_eur_mwh": [7.0, 80.0],
                       "min_gen_frac": [0.0, 0.0]})
_FLEX = {"N": {"idx": [0], "alpha_band": [0.60], "alpha_tech": [0.25], "c_mod": 8.0, "c_start": [2000.0]}}


def _prices(flex, demand=(38000, 38000, 18000, 38000)):
    T = pd.date_range("2024-05-19", periods=len(demand), freq="h", tz="UTC")
    zd = {"N": {"stack": _STACK, "demand": list(map(float, demand)), "res_pot": [0.0] * len(demand),
                "avail": None, "energy_caps": None}}
    return solve_multizone_highs(T, zd, [], {}, res_bid=-10.0, price_floor=-500.0, flex=flex)["prices"]["N"]


def test_no_flex_has_no_negative_and_sits_at_nuclear_srmc():
    pr = _prices(None)
    assert (pr.to_numpy() >= 0).all()                    # no rigidity → nuclear freely follows the trough
    assert abs(pr.iloc[2] - 7.0) < 1e-6                  # trough priced at nuclear SRMC


def test_committed_reactor_deep_modulates_to_a_negative_price():
    pr = _prices(_FLEX)
    # held at u≈38 GW, the band floor 0.6·u ≈ 22.8 GW exceeds the 18 GW trough → the reactor must deep-
    # modulate at the margin. Its implicit bid to produce the marginal MWh is fuel cost minus the modulation
    # cost it thereby avoids: srmc − c_mod = 7 − 8 = −1. The balance dual is that negative bid.
    assert pr.iloc[2] < 0
    assert abs(pr.iloc[2] - (7.0 - 8.0)) < 1.0           # ≈ srmc − c_mod


def test_negative_depth_scales_with_c_mod():
    flex = {"N": {**_FLEX["N"], "c_mod": 25.0}}
    pr = _prices(flex)
    assert abs(pr.iloc[2] - (7.0 - 25.0)) < 1.0          # deeper modulation cost → deeper negative print


def test_no_recovery_lets_the_reactor_shut_without_a_negative():
    # monotonically falling demand: the trough is at the window end, so u can be shed for free (no recommit)
    pr = _prices(_FLEX, demand=(38000, 38000, 22000, 16000))
    assert (pr.to_numpy() >= -1e-6).all()               # C5 coupling: stickiness needs a recovery to bite
