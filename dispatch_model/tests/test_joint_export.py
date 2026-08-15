"""Per-zone simultaneous-export row — `zones_data[z]["export_cap"]` in `lp.highs_solver._build`.

A zone's borders share internal network elements, so what limits simultaneous export is a shared element
and the constraint belongs on the SUM. `flow_derived_ntc` approximates that with one coincidence factor
applied to every border, which is a BOX approximation to a SIMPLEX: it forbids one corridor running at its
own revealed capability while the others idle, and only touches the true constraint at a corner.

Measured on IT-North, whose NORD->CNOR corridor carries 91 % of its exports and is ANTI-correlated with the
others: p99.5 = 4210 MW, derated to 1843, exceeded by observed flow in 41.8 % of 2024 hours. In its top-100
export hours northern Italy imports 6470 MW across the Alps while exporting 4146 MW south — it TRANSITS
rather than competing for one export budget.

The decisive property is `test_one_corridor_may_use_its_full_capability`: it passes with the row and fails
under any uniform per-border derating that reproduces the same total.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from dispatch_model.lp.highs_solver import solve_multizone_highs

_T = pd.date_range("2024-05-19", periods=3, freq="h", tz="UTC")


def _stack(cap, srmc):
    return pd.DataFrame({"unit_id": ["G"], "tech": ["gas"], "capacity_mw": [float(cap)],
                         "srmc_eur_mwh": [float(srmc)], "min_gen_frac": [0.0]})


def _zone(cap, srmc, demand, export_cap=None):
    z = {"stack": _stack(cap, srmc), "demand": [float(demand)] * len(_T),
         "res_pot": [0.0] * len(_T), "avail": None, "energy_caps": None}
    if export_cap is not None:
        z["export_cap"] = float(export_cap)
    return z


def _solve(zd, borders, ntc):
    return solve_multizone_highs(_T, zd, borders, ntc, res_bid=-10.0, price_floor=-500.0)


def _exports(out, z, borders):
    """Total MW exported from `z` in hour 0, from the returned flow frame."""
    f = out["flows"]
    tot = 0.0
    for a, b in borders:
        row = f[(f["border"] == f"{a}>{b}") & (f["time"] == _T[0])]
        if row.empty:
            continue
        v = float(row["flow_mw"].iloc[0])
        tot += v if a == z else 0.0
    return tot


def test_absent_export_cap_adds_no_row_and_changes_nothing():
    """Flag-off must be byte-identical: a zone without `export_cap` gets no row at all."""
    borders = [("A", "B")]
    ntc = {("A", "B"): (5000.0, 5000.0)}
    zd_no = {"A": _zone(9000, 5.0, 1000), "B": _zone(9000, 90.0, 5000)}
    zd_none = {"A": _zone(9000, 5.0, 1000, export_cap=None), "B": _zone(9000, 90.0, 5000)}
    a = _solve(zd_no, borders, ntc)["prices"]["B"]
    b = _solve(zd_none, borders, ntc)["prices"]["B"]
    assert np.allclose(np.asarray(a, float), np.asarray(b, float), atol=1e-9)


def test_the_row_binds_the_total_not_each_border():
    """A owns cheap generation and two hungry neighbours; its total export may not exceed the cap."""
    borders = [("A", "B"), ("A", "C")]
    ntc = {("A", "B"): (4000.0, 4000.0), ("A", "C"): (4000.0, 4000.0)}
    zd = {"A": _zone(20000, 5.0, 1000, export_cap=3000.0),
          "B": _zone(9000, 90.0, 4000), "C": _zone(9000, 90.0, 4000)}
    out = _solve(zd, borders, ntc)
    tot = _exports(out, "A", borders)
    assert tot <= 3000.0 + 1e-6, f"joint row not binding: A exported {tot:.0f} MW against a 3000 cap"
    assert tot > 2900.0, "the cap should bind, not sit idle — B and C are both short and expensive"


def test_one_corridor_may_use_its_full_capability():
    """THE property, and the reason for the change.

    Only C is short; B needs nothing. The A->C corridor must be allowed to carry the zone's whole export
    budget. A uniform per-border derating reproducing the same 3000 MW total would cap EACH corridor at
    1500 and strand half of it — that is the Italian transit case in miniature.
    """
    borders = [("A", "B"), ("A", "C")]
    ntc = {("A", "B"): (4000.0, 4000.0), ("A", "C"): (4000.0, 4000.0)}
    zd = {"A": _zone(20000, 5.0, 1000, export_cap=3000.0),
          "B": _zone(9000, 5.0, 500),                    # self-sufficient and cheap: wants no imports
          "C": _zone(9000, 90.0, 6000)}                  # short and expensive: wants everything
    out = _solve(zd, borders, ntc)
    f = out["flows"]
    ac = float(f[(f["border"] == "A>C") & (f["time"] == _T[0])]["flow_mw"].iloc[0])
    assert ac > 2900.0, (
        f"A->C carried only {ac:.0f} MW of a 3000 MW zone budget; the row must let ONE corridor take it all")


def test_cap_above_border_capacity_is_slack():
    """A cap larger than what the borders can carry must not distort anything."""
    borders = [("A", "B")]
    ntc = {("A", "B"): (1200.0, 1200.0)}
    zd_hi = {"A": _zone(20000, 5.0, 1000, export_cap=99000.0), "B": _zone(9000, 90.0, 5000)}
    zd_no = {"A": _zone(20000, 5.0, 1000), "B": _zone(9000, 90.0, 5000)}
    a = np.asarray(_solve(zd_hi, borders, ntc)["prices"]["B"], float)
    b = np.asarray(_solve(zd_no, borders, ntc)["prices"]["B"], float)
    assert np.allclose(a, b, atol=1e-9)


@pytest.mark.parametrize("cap", [0.0, -5.0, float("nan")])
def test_degenerate_caps_are_ignored_not_applied(cap):
    """A zero/negative/NaN cap means 'no information', not 'this zone may not export'."""
    borders = [("A", "B")]
    ntc = {("A", "B"): (4000.0, 4000.0)}
    zd = {"A": _zone(20000, 5.0, 1000, export_cap=cap), "B": _zone(9000, 90.0, 5000)}
    ref = {"A": _zone(20000, 5.0, 1000), "B": _zone(9000, 90.0, 5000)}
    a = np.asarray(_solve(zd, borders, ntc)["prices"]["B"], float)
    b = np.asarray(_solve(ref, borders, ntc)["prices"]["B"], float)
    assert np.allclose(a, b, atol=1e-9)


def test_the_import_row_binds_the_total_not_each_border():
    """Mirror of the export row. Z is short and has two cheap neighbours; its total import is capped."""
    borders = [("B", "A"), ("C", "A")]
    ntc = {("B", "A"): (4000.0, 4000.0), ("C", "A"): (4000.0, 4000.0)}
    zd = {"A": dict(_zone(2000, 300.0, 8000), import_cap=3000.0),
          "B": _zone(20000, 5.0, 500), "C": _zone(20000, 5.0, 500)}
    out = _solve(zd, borders, ntc)
    f = out["flows"]
    imp = sum(float(f.query(f"border == '{a}>{b}'").iloc[0]["flow_mw"]) for a, b in borders)
    assert imp <= 3000.0 + 1e-6, f"import row not binding: A imported {imp:.0f} MW against a 3000 cap"
    assert imp > 2900.0, "the cap should bind — A is short and both neighbours are cheap"


def test_one_import_corridor_may_use_its_full_capability():
    """The simplex property again, on the import side: one idle neighbour must not strand A's budget."""
    borders = [("B", "A"), ("C", "A")]
    ntc = {("B", "A"): (4000.0, 4000.0), ("C", "A"): (4000.0, 4000.0)}
    zd = {"A": dict(_zone(2000, 300.0, 8000), import_cap=3000.0),
          "B": _zone(20000, 400.0, 500),                 # dearer than A: will not export
          "C": _zone(20000, 5.0, 500)}                   # cheap: should carry the whole budget
    out = _solve(zd, borders, ntc)
    ca = float(out["flows"].query("border == 'C>A'").iloc[0]["flow_mw"])
    assert ca > 2900.0, f"C->A carried only {ca:.0f} MW of a 3000 MW import budget"


def test_export_and_import_rows_are_independent():
    """A zone carrying both caps must respect each on its own, not their sum."""
    borders = [("A", "B")]
    ntc = {("A", "B"): (4000.0, 4000.0)}
    zd = {"A": dict(_zone(20000, 5.0, 1000), export_cap=1500.0, import_cap=50.0),
          "B": _zone(9000, 90.0, 5000)}
    net = float(_solve(zd, borders, ntc)["flows"].query("border == 'A>B'").iloc[0]["flow_mw"])
    assert 1400.0 < net <= 1500.0 + 1e-6, f"export cap not respected independently: net {net:.0f}"


def test_export_row_does_not_constrain_imports():
    """It is an EXPORT row: a zone importing heavily must be untouched by its own cap.

    `flow_mw` is the NET border flow, so a negative `A>B` is B exporting into A. A carries a deliberately
    tiny export cap of 100 MW and must still import at its full 4000 MW NTC. (A's price stays at its own
    gas SRMC because 4000 MW of imports do not cover 6000 MW of demand — that is correct, not a symptom.)
    """
    borders = [("A", "B")]
    ntc = {("A", "B"): (4000.0, 4000.0)}
    zd = {"A": _zone(9000, 90.0, 6000, export_cap=100.0),
          "B": _zone(20000, 5.0, 1000)}
    out = _solve(zd, borders, ntc)
    net = float(out["flows"].query("border == 'A>B'").iloc[0]["flow_mw"])
    assert net < -3999.0, f"A's export cap blocked its IMPORTS: net flow {net:.0f} MW, expected -4000"


def test_legs_restrict_which_borders_the_row_bounds():
    """The narrow version: only the named legs count toward the zone's budget.

    Borders carrying a PUBLISHED day-ahead NTC already have a commercial authority (`hourly_ntc` overrides
    the derived scalar there), so including them would let published flows consume a budget meant to bound
    the residue. Here A->B is 'published' and excluded; only A->C is bounded.
    """
    borders = [("A", "B"), ("A", "C")]
    ntc = {("A", "B"): (4000.0, 4000.0), ("A", "C"): (4000.0, 4000.0)}
    zd = {"A": dict(_zone(20000, 5.0, 1000), export_cap=1000.0, export_legs=["C"]),
          "B": _zone(9000, 90.0, 4000), "C": _zone(9000, 90.0, 4000)}
    f = _solve(zd, borders, ntc)["flows"]
    ab = float(f.query("border == 'A>B'").iloc[0]["flow_mw"])
    ac = float(f.query("border == 'A>C'").iloc[0]["flow_mw"])
    assert ac <= 1000.0 + 1e-6, f"the bounded leg exceeded its budget: A->C {ac:.0f}"
    assert ab > 2000.0, f"the EXCLUDED leg was constrained too: A->B only {ab:.0f} MW"


def test_empty_leg_list_means_no_row_rather_than_a_zero_budget():
    """A zone all of whose borders are published has nothing left to bound — it must get no row."""
    borders = [("A", "B")]
    ntc = {("A", "B"): (4000.0, 4000.0)}
    zd = {"A": dict(_zone(20000, 5.0, 1000), export_cap=1000.0, export_legs=[]),
          "B": _zone(9000, 90.0, 5000)}
    ref = {"A": _zone(20000, 5.0, 1000), "B": _zone(9000, 90.0, 5000)}
    a = np.asarray(_solve(zd, borders, ntc)["prices"]["B"], float)
    b = np.asarray(_solve(ref, borders, ntc)["prices"]["B"], float)
    assert np.allclose(a, b, atol=1e-9), "an empty leg list must disable the row, not forbid all export"
