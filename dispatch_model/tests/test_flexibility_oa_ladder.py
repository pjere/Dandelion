"""FLEX-F4 §6 — the FR downward bid ladder (`trajectories.apply_oa_ladder`). Maps RES scheme tranches onto
the CR/OA/merchant bid levels and truncates to the market floor/cap, leaving shares & triggers (the vintage
decay) untouched."""
from __future__ import annotations

from dispatch_model.flexibility.trajectories import _DEFAULT_OA_LADDER, apply_oa_ladder

_LADDER = {"cr_bid": -1.0, "oa_bid": -500.0, "market_floor": -500.0, "market_cap": 4000.0}
_FR = [{"scheme": "complement_remuneration", "share": 0.55, "floor": -5.0, "trigger": 1},
       {"scheme": "obligation_achat", "share": 0.25, "floor": -40.0, "trigger": 0},
       {"scheme": "merchant", "share": 0.20, "floor": 0.0, "trigger": 0}]


def test_ladder_repricing_by_scheme():
    out = {t["scheme"]: t for t in apply_oa_ladder(_FR, _LADDER)}
    assert out["complement_remuneration"]["floor"] == -1.0     # CR ≈0 (premium suspended below zero)
    assert out["obligation_achat"]["floor"] == -500.0          # OA paid regardless → bids at the market floor
    assert out["merchant"]["floor"] == 0.0


def test_shares_and_triggers_are_preserved():
    out = apply_oa_ladder(_FR, _LADDER)
    assert [t["share"] for t in out] == [0.55, 0.25, 0.20]     # vintage decay (volume) untouched
    assert [t["trigger"] for t in out] == [1, 0, 0]


def test_bids_are_truncated_to_the_market_bounds():
    ladder = {**_LADDER, "oa_bid": -900.0, "market_floor": -500.0}   # OA below the floor → clamped up to it
    out = {t["scheme"]: t for t in apply_oa_ladder(_FR, ladder)}
    assert out["obligation_achat"]["floor"] == -500.0
    capped = apply_oa_ladder([{"scheme": "x", "share": 1.0, "floor": 9000.0, "trigger": 0}], _LADDER)
    assert capped[0]["floor"] == 4000.0                        # unknown scheme kept but clamped to market_cap


def test_default_ladder_is_sourced_correct():
    # absent dispatch_oa_ladder tab → sourced defaults: CR ≈0 (premium suspended), OA at the market floor.
    assert _DEFAULT_OA_LADDER["oa_bid"] == -500.0
    assert -5.0 <= _DEFAULT_OA_LADDER["cr_bid"] <= 0.0
