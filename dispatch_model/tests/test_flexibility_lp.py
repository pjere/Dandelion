"""FLEX-F2 — nuclear rigidity core in the LP (C1 two-tier min + C2 c_mod + C5 start cost). Synthetic,
no DB: assert the balance duals go negative exactly when a committed reactor is forced into surplus."""
from __future__ import annotations

import numpy as np
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


# ---- FLEX-F2b: intertemporal rationing (C2 budgets, C3 xénon ramp, C5 min-down) ------------------------
# A multi-day window so the 8h/daily budgets and the ramps have room to bite. One flex reactor + gas; a
# deep, sustained midday trough on each day, framed by high shoulders so C5's start cost holds `u` up.
def _run(flex, demand, srmc_nuc=7.0):
    T = pd.date_range("2024-05-19", periods=len(demand), freq="h", tz="UTC")
    stack = pd.DataFrame({"unit_id": ["NUC", "GAS"], "tech": ["nuclear", "gas"],
                          "capacity_mw": [40000.0, 12000.0], "srmc_eur_mwh": [srmc_nuc, 80.0],
                          "min_gen_frac": [0.0, 0.0]})
    zd = {"N": {"stack": stack, "demand": list(map(float, demand)), "res_pot": [0.0] * len(demand),
                "avail": None, "energy_caps": None}}
    return solve_multizone_highs(T, zd, [], {}, res_bid=-10.0, price_floor=-500.0, flex=flex)


def _two_day_trough():
    day = [38000, 38000, 30000, 20000, 16000, 16000, 20000, 30000, 38000, 38000, 38000, 38000]  # 12 h
    return day * 4                                        # 48 h = two 24-h calendar days (~38.4 GWh/day of
    #                                                      unconstrained deep-mod at the trough)


_FLEX2B = {"idx": [0], "alpha_band": [0.60], "alpha_tech": [0.25], "c_mod": 8.0, "c_start": [50000.0]}


def test_daily_deepmod_budget_caps_cumulative_modulation():
    dem = _two_day_trough()
    loose = _run({"N": {**_FLEX2B, "d_max_day": [2.0]}}, dem)    # 2·cap = above the natural daily deep-mod
    tight = _run({"N": {**_FLEX2B, "d_max_day": [0.5]}}, dem)    # 0.5·cap MWh of deep-mod per calendar day
    cap = 40000.0
    d_tight = tight["flex"]["N"]["d"][0]
    for day_slice in (slice(0, 24), slice(24, 48)):             # each calendar day respects its budget
        assert d_tight[day_slice].sum() <= 0.5 * cap + 1e-3
    # and the budget genuinely bit: the loose run modulated strictly more energy
    assert loose["flex"]["N"]["d"].sum() > tight["flex"]["N"]["d"].sum() + 1.0


def test_rolling_8h_budget_caps_every_8h_window():
    dem = _two_day_trough()
    out = _run({"N": {**_FLEX2B, "d_max_8h": [0.3]}}, dem)      # 0.3·cap MWh over any rolling 8 h
    d = out["flex"]["N"]["d"][0]
    win = np.convolve(d, np.ones(8), mode="valid")             # every 8-consecutive-hour deep-mod sum
    assert win.max() <= 0.3 * 40000.0 + 1e-3


def test_exhausted_deepmod_budget_pushes_the_trough_to_the_floor():
    # With deep-mod rationed, the reactor cannot absorb the whole midday surplus; the residual must be
    # dumped, so the trough hour prints at the price floor instead of the shallow srmc − c_mod.
    dem = _two_day_trough()
    tight = _run({"N": {**_FLEX2B, "d_max_day": [0.3]}}, dem)
    assert tight["prices"]["N"].min() < -100.0                  # collapses toward the −500 floor


def test_xenon_beta_throttles_the_post_modulation_up_ramp():
    dem = _two_day_trough()
    base = {**_FLEX2B, "d_max_day": [6.0], "r_up": [0.30]}
    no_x = _run({"N": {**base, "xenon_beta": [0.0]}}, dem)
    with_x = _run({"N": {**base, "xenon_beta": [0.60]}}, dem)
    # peak hourly output rise of the reactor over the window: xénon must not let it climb faster
    ramp_no_x = np.diff(no_x["flex"]["N"]["p"][0]).max()
    ramp_with_x = np.diff(with_x["flex"]["N"]["p"][0]).max()
    assert ramp_with_x < ramp_no_x - 1.0


def test_min_down_persistence_bounds_the_recommit_ramp():
    # A deep trough deep enough that shutting is on the table; ρ_recommit caps how fast `u` climbs back,
    # so the committed capacity cannot rise by more than avail·ρ between consecutive hours.
    dem = _two_day_trough()
    rho = 0.10
    out = _run({"N": {**_FLEX2B, "c_start": [10.0], "rho_recommit": [rho]}}, dem)
    u = out["flex"]["N"]["u"][0]
    assert np.diff(u).max() <= rho * 40000.0 + 1e-3


# ---- FLEX-F3: maneuverability (C4), reserves (C6), grid-stability floor (C7) --------------------------
def test_c4_none_maneuverability_freezes_output_at_stretch_power():
    # 'none' (end-of-cycle stretch-out) pins p at stretch·avail·cap — must-run, no modulation into the trough.
    dem = _two_day_trough()
    out = _run({"N": {**_FLEX2B, "must_run_frac": [0.9]}}, dem)
    p = out["flex"]["N"]["p"][0]
    assert np.allclose(p, 0.9 * 40000.0, atol=1.0)             # frozen flat at 36 GW regardless of demand


def test_c4_reduced_maneuverability_halves_the_deep_band():
    # deepband_scale 0.5 halves how far the reactor can deep-modulate below the band floor (C1b).
    dem = _two_day_trough()
    full = _run({"N": {**_FLEX2B, "d_max_day": [24.0]}}, dem)
    red = _run({"N": {**_FLEX2B, "d_max_day": [24.0], "deepband_scale": [0.5]}}, dem)
    assert red["flex"]["N"]["d"][0].max() < full["flex"]["N"]["d"][0].max() - 1.0
    assert red["flex"]["N"]["d"][0].max() <= 0.5 * (0.60 - 0.25) * 40000.0 + 1.0


def test_c7_grid_stability_floor_holds_nuclear_output_up():
    # a deep trough that would otherwise let the fleet fall to its band floor; P_minstab pins Σp ≥ floor.
    dem = _two_day_trough()
    floor = 30000.0
    out = _run({"N": {**_FLEX2B, "d_max_day": [24.0], "p_minstab": floor, "is_nuclear": [True]}}, dem)
    assert out["flex"]["N"]["p"][0].min() >= floor - 1.0       # never dips below the stability floor


def test_c6_upward_headroom_reserve_commits_spare_capacity():
    # headroom = u − p (committed but unproduced, rampable up). R_up_req forces the fleet to over-commit at
    # the peak so it keeps that much upward reserve in hand.
    dem = _two_day_trough()
    req = 2000.0
    base = _run({"N": {**_FLEX2B, "d_max_day": [24.0]}}, dem)
    resv = _run({"N": {**_FLEX2B, "d_max_day": [24.0], "r_up_req": req}}, dem)
    hb = base["flex"]["N"]["u"][0] - base["flex"]["N"]["p"][0]
    hr = resv["flex"]["N"]["u"][0] - resv["flex"]["N"]["p"][0]
    assert hb.min() < req                                     # base runs a unit flat-out at the peak (no headroom)
    assert hr.min() >= req - 1.0                              # C6 keeps ≥ req of headroom every hour


# ---- FLEX-F5: window-seam state linking (u_init / p_init / d_hist as fixed pre-window parameters) --------
_SEAM_BASE = {"idx": [0], "alpha_band": [0.60], "alpha_tech": [0.25], "c_mod": 8.0, "c_start": [2000.0]}
_HIGH = (38000, 38000, 38000, 38000)


def test_f5_c3_ramp_seam_bounds_the_first_hour_climb():
    spec = {**_SEAM_BASE, "r_up": [0.05], "xenon_beta": [0.0]}
    no_seam = _run({"N": spec}, _HIGH)
    seam = _run({"N": {**spec, "u_init": [40000.0], "p_init": [10000.0], "d_hist": [[0.0] * 8]}}, _HIGH)
    assert no_seam["flex"]["N"]["p"][0][0] > 30000        # no seam → the first hour freely serves the load
    p0 = seam["flex"]["N"]["p"][0][0]; u0 = seam["flex"]["N"]["u"][0][0]
    assert p0 < 15000                                     # the seam ramp caps the climb from p_init
    assert p0 <= 10000.0 + 0.05 * u0 + 1.0                # p_0 − r_up·u_0 ≤ p_init


def test_f5_c5_seam_charges_a_start_when_the_reactor_was_off():
    off = _run({"N": {**_SEAM_BASE, "u_init": [0.0], "p_init": [0.0], "d_hist": [[0.0] * 8]}}, _HIGH)
    on = _run({"N": {**_SEAM_BASE, "u_init": [40000.0], "p_init": [38000.0], "d_hist": [[0.0] * 8]}}, _HIGH)
    assert off["flex"]["N"]["su"][0][0] > 20000           # committing from cold pays a large start at t=0
    assert on["flex"]["N"]["su"][0][0] < 1.0              # already committed across the seam → no start


def test_f5_min_down_seam_bounds_recommit_from_a_low_state():
    spec = {**_SEAM_BASE, "c_start": [10.0], "rho_recommit": [0.10]}
    out = _run({"N": {**spec, "u_init": [5000.0], "p_init": [3000.0], "d_hist": [[0.0] * 8]}}, _HIGH)
    assert out["flex"]["N"]["u"][0][0] <= 0.10 * 40000.0 + 5000.0 + 1.0    # u_0 ≤ avail_0·ρ + u_init


def test_f5_seam_budget_clamp_stays_feasible_when_history_exceeds_budget():
    # a maneuverability drop across the seam can leave d_hist above the tightened 8h budget; the clamp must
    # keep the window feasible (reactor can't deep-mod until the history rolls out), not raise.
    dem = (16000, 16000, 38000, 38000)                    # deep trough at t=0 → the reactor *wants* to deep-mod
    spec = {**_SEAM_BASE, "c_start": [50000.0], "d_max_8h": [0.05],
            "u_init": [40000.0], "p_init": [38000.0], "d_hist": [[8000.0] * 8]}   # 64 GWh ≫ 2 GWh budget
    out = _run({"N": spec}, dem)                           # solves (no infeasibility) …
    assert out["flex"]["N"]["d"][0][0] <= 1.0             # … with hour-0 deep-mod pinned to 0 (budget spent)


def test_f5_xenon_history_tightens_the_seam_ramp():
    spec = {**_SEAM_BASE, "r_up": [0.30], "xenon_beta": [0.50]}
    clean = _run({"N": {**spec, "u_init": [40000.0], "p_init": [20000.0], "d_hist": [[0.0] * 8]}}, _HIGH)
    poisoned = _run({"N": {**spec, "u_init": [40000.0], "p_init": [20000.0], "d_hist": [[3000.0] * 8]}}, _HIGH)
    assert poisoned["flex"]["N"]["p"][0][0] < clean["flex"]["N"]["p"][0][0] - 1.0   # recent deep-mod slows the climb


def test_c6_downward_footroom_reserve_is_held_above_the_technical_minimum():
    # footroom = p − α_tech·u (room to be commanded down). R_down_req forces the fleet to keep that much in
    # hand; the unconstrained base runs it down below that at the trough.
    dem = _two_day_trough()
    req = 10000.0
    at = 0.25
    base = _run({"N": {**_FLEX2B, "d_max_day": [24.0]}}, dem)
    resv = _run({"N": {**_FLEX2B, "d_max_day": [24.0], "r_down_req": req}}, dem)
    fb = base["flex"]["N"]["p"][0] - at * base["flex"]["N"]["u"][0]
    fr = resv["flex"]["N"]["p"][0] - at * resv["flex"]["N"]["u"][0]
    assert fb.min() < req                                      # base doesn't hold this much downward reserve
    assert fr.min() >= req - 1.0                               # C6 holds it every hour
