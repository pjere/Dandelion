"""`res_schemes._zone_tranches` — no RES tranche may bid exactly 0.00.

A tranche at exactly zero is a ZERO-COST CURTAILMENT SINK. The LP minimises `sum(floor_i * res_i)` and so
curtails the least-negative tranche first; while that tranche is partially curtailed it is marginal and the
balance dual equals its bid. At 0.00 the zone therefore prints exactly 0.000 for any surplus that fits
inside the tranche, and negative prices become unreachable regardless of how much surplus exists.

Measured on Spain: the merchant rung held 38 % of RES at 0.00 against rungs of -1.01/-5.80 below it, giving
1513 pooled hours at exactly zero and ZERO hours below -0.05, against 803 observed negative hours. It leaked
into Portugal too — all 186 PT "negatives" were exactly -0.001, the wheeling penalty on Spain's zero.
"""
from __future__ import annotations

import numpy as np
import pytest

from dispatch_model.res_schemes import _MERCHANT_BID, _zone_tranches


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    """The fix is HELD OFF by default (see `_MERCHANT_BID`); these tests pin its behaviour."""
    monkeypatch.setenv("DISPATCH_NO_ZERO_RES_BID", "1")


def _floors(tranches):
    return sorted(float(t["floor"][0]) for t in tranches)


def test_a_zero_scheme_tranche_is_repriced_below_zero():
    schemes = {"ES": [{"scheme": "recore", "share": 0.62, "floor": -1.01, "trigger": 0},
                      {"scheme": "merchant", "share": 0.38, "floor": 0.00, "trigger": 0}]}
    tr = _zone_tranches("ES", schemes, -10.0, 24)
    assert 0.0 not in _floors(tr), "a tranche at exactly 0.00 absorbs surplus without ever pricing negative"
    assert _MERCHANT_BID < 0.0
    assert _floors(tr) == sorted([-1.01, _MERCHANT_BID])


def test_nonzero_floors_are_untouched():
    schemes = {"DE_LU": [{"scheme": "fit", "share": 0.3, "floor": -300.0, "trigger": 0},
                         {"scheme": "market_premium", "share": 0.6, "floor": -20.0, "trigger": 6},
                         {"scheme": "merchant", "share": 0.1, "floor": -2.0, "trigger": 0}]}
    assert _floors(_zone_tranches("DE_LU", schemes, -10.0, 24)) == [-300.0, -20.0, -2.0]


def test_the_REGULATORY_floor_stays_at_exactly_zero():
    """The one place zero is correct. ES before Dec-2023 and IT-North before the Jan-2025 TIDE reform
    could not print negative AT ALL — that is a market rule, not a bid, and repricing it would invent
    prices the market was forbidden to make."""
    tr = _zone_tranches("ES", {"ES": [{"scheme": "recore", "share": 1.0, "floor": 0.0, "trigger": 0}]},
                        0.0, 24)                       # res_bid >= 0 ⇒ the regulatory branch
    assert len(tr) == 1 and tr[0]["scheme"] == "floored"
    assert float(tr[0]["floor"][0]) == 0.0, "a regulatory prohibition must stay at exactly zero"


def test_merchant_fallback_can_never_produce_a_zero_bid():
    """A zone with no scheme rows falls back to one merchant tranche at its `res_bid`.

    That path is already safe, and it is worth pinning why: a `res_bid` of 0 or -0.0 takes the REGULATORY
    branch first (`-0.0 >= 0` is True in Python), and `None` falls back to -10.0. So the fallback cannot
    reach zero from any input.
    """
    assert float(_zone_tranches("XX", {}, None, 24)[0]["floor"][0]) == -10.0
    assert float(_zone_tranches("XX", {}, -3.5, 24)[0]["floor"][0]) == -3.5
    zeroed = _zone_tranches("XX", {}, -0.0, 24)          # regulatory branch, correctly stays at 0
    assert zeroed[0]["scheme"] == "floored" and float(zeroed[0]["floor"][0]) == 0.0


@pytest.mark.parametrize("zone,share", [("ES", 0.38), ("CH", 0.25)])
def test_the_sink_is_gone_for_every_zone_that_carried_one(zone, share):
    """ES (0.38) and CH (0.25) both carried a merchant rung at exactly 0.00 in the workbook."""
    schemes = {zone: [{"scheme": "sub", "share": 1 - share, "floor": -50.0, "trigger": 0},
                      {"scheme": "merchant", "share": share, "floor": 0.00, "trigger": 0}]}
    assert all(f < 0.0 for f in _floors(_zone_tranches(zone, schemes, -10.0, 24)))


def test_shares_and_triggers_survive_the_repricing():
    schemes = {"ES": [{"scheme": "merchant", "share": 0.38, "floor": 0.00, "trigger": 6}]}
    tr = _zone_tranches("ES", schemes, -10.0, 12)
    assert tr[0]["share"] == 0.38 and tr[0]["trigger"] == 6
    assert len(tr[0]["floor"]) == 12 and np.all(tr[0]["floor"] == _MERCHANT_BID)
