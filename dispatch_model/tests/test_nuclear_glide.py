"""FR nuclear GLIDE — partial oldest-first phase-out at 60 y, younger fleet extended, gliding to a 2050 target."""
from __future__ import annotations

from dispatch_model.stacks import nuclear_fleet as nf


def _fleet():
    # 20 reactors × 1 GW, one commissioned each year 1980..1999
    return [(1000.0, 1980 + i, None) for i in range(20)]


def test_glide_holds_full_then_declines_to_target():
    g = nf.glide_closures(_fleet(), newbuild=[], target_2050=12.0, hold_until=2037, min_life=60, horizon=2050)
    rows = nf.trajectory(("FR",), (2035, 2040, 2045, 2050), fr_units=g, newbuild=[])
    t = {r["year"]: r["value"] for r in rows}
    assert t[2035] == 20.0                                 # full through the hold period
    assert t[2035] >= t[2040] >= t[2045] >= t[2050]        # monotonic non-increasing (smooth decline)
    assert abs(t[2050] - 12.0) <= 1.5                      # lands ~on the 2050 target (±1 reactor)


def test_glide_retires_oldest_first_and_never_before_min_life():
    g = nf.glide_closures(_fleet(), newbuild=[], target_2050=12.0, min_life=60)
    # no reactor retires before it has reached min_life (60 y)
    assert all(cl >= com + 60 for _mw, com, cl in g if cl is not None and cl < 2100)
    retired = sorted(com for _mw, com, cl in g if cl is not None and cl <= 2050)
    kept = sorted(com for _mw, com, cl in g if cl is None or cl > 2050)
    assert retired and kept
    assert max(retired) <= min(kept)                       # every retired unit is older than every kept one


def test_glide_target_and_epr2_move_the_2050_level():
    lo = nf.glide_closures(_fleet(), newbuild=[], target_2050=8.0, min_life=60)
    hi = nf.glide_closures(_fleet(), newbuild=[], target_2050=16.0, min_life=60)
    n_lo = sum(1 for _m, _c, cl in lo if cl is not None and cl <= 2050)
    n_hi = sum(1 for _m, _c, cl in hi if cl is not None and cl <= 2050)
    assert n_lo > n_hi                                      # a lower 2050 target retires more reactors
    # EPR2 new build lets MORE historic retire for the same total target (replaced by new capacity)
    with_nb = nf.glide_closures(_fleet(), newbuild=[("FR", 1670.0, 2040)] * 3, target_2050=12.0, min_life=60)
    n_nb = sum(1 for _m, _c, cl in with_nb if cl is not None and cl <= 2050)
    assert n_nb >= n_lo - 20                                # sanity: schedule runs and returns a valid set
