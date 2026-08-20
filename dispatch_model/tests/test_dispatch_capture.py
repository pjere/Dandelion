"""`DISPATCH_CAPTURE_DISPATCH` — reading the volume behind each price out of the LP primal.

The gate and the backtest score prices, so the solver never had to return volumes. A projection that has to
report REVENUES does: a price is only worth what it is paid on. This capture reads the generation, RES,
ENS, dump and storage columns that HiGHS already solved for, aggregates them per (zone, tech), and changes
nothing about the LP.

Two properties are pinned because their failure would be silent and would corrupt every downstream number:

1. **The flag off is invisible.** Prices must be bit-identical and the key absent — a capture that perturbed
   the dispatch would poison the price series this repo has spent its whole history calibrating.
2. **The captured volumes close the balance.** Column-block arithmetic is index arithmetic on a flat primal
   vector, and an off-by-one in a base offset or a wrong reshape order (unit-major vs time-major) produces
   numbers that look entirely reasonable — right magnitude, right sign, wrong plant. Asserting the LP's own
   balance row against the capture is the check that a plausible-looking mistake cannot pass.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from dispatch_model.lp.multi_zone import solve_multizone

_A = pd.DataFrame([("A_NUC", "nuclear", 40000, 7.0, 0.0), ("A_GAS", "gas", 10000, 40.0, 0.0),
                   ("A_GAS2", "gas", 6000, 55.0, 0.0)],
                  columns=["unit_id", "tech", "capacity_mw", "srmc_eur_mwh", "min_gen_frac"])
_B = pd.DataFrame([("B_GAS", "gas", 20000, 80.0, 0.0), ("B_COAL", "coal", 8000, 60.0, 0.0)],
                  columns=["unit_id", "tech", "capacity_mw", "srmc_eur_mwh", "min_gen_frac"])


def _run(ntc=5000, res_a=3000.0, dA=30000, dB=24000, hours=6):
    T = pd.date_range("2024-01-01", periods=hours, freq="h", tz="UTC")
    zd = {"A": {"stack": _A, "demand": np.full(hours, dA, float),
                "res_pot": np.full(hours, res_a, float)},
          "B": {"stack": _B, "demand": np.full(hours, dB, float),
                "res_pot": np.zeros(hours)}}
    return solve_multizone(T, zd, [("A", "B")], {("A", "B"): (ntc, ntc)}), T


def test_flag_off_leaves_no_trace(monkeypatch):
    monkeypatch.delenv("DISPATCH_CAPTURE_DISPATCH", raising=False)
    out, _ = _run()
    assert "dispatch" not in out


def test_prices_are_identical_with_and_without_capture(monkeypatch):
    monkeypatch.delenv("DISPATCH_CAPTURE_DISPATCH", raising=False)
    off, _ = _run()
    monkeypatch.setenv("DISPATCH_CAPTURE_DISPATCH", "1")
    on, _ = _run()
    pd.testing.assert_frame_equal(off["prices"], on["prices"])
    assert (off["flows"]["flow_mw"].to_numpy() == on["flows"]["flow_mw"].to_numpy()).all()


def test_capture_closes_the_zonal_balance(monkeypatch):
    """gen + res + ens − dump + net_import = demand, per zone-hour.

    This is the LP's own balance row restated from the capture. It fails on any block-offset or reshape
    error, which is exactly the class of bug that produces believable wrong revenue."""
    monkeypatch.setenv("DISPATCH_CAPTURE_DISPATCH", "1")
    out, T = _run()
    d = out["dispatch"]
    flow = out["flows"].pivot(index="time", columns="border", values="flow_mw")["A>B"].reindex(T)
    for zone, demand, net_import in (("A", 30000.0, -flow), ("B", 24000.0, flow)):
        got = d[zone].drop(columns=["dump"]).sum(axis=1) - d[(zone, "dump")] + net_import.to_numpy()
        assert np.allclose(got.to_numpy(), demand, atol=1e-5), \
            f"{zone} balance off by {np.abs(got.to_numpy() - demand).max():.3f} MW"


def test_units_are_aggregated_by_tech_not_double_counted(monkeypatch):
    """Zone A has two gas units. The gas column must be their SUM, and the whole capture must not exceed
    the fleet — a time-major reshape would silently mix units and still sum to something."""
    monkeypatch.setenv("DISPATCH_CAPTURE_DISPATCH", "1")
    out, _ = _run()
    d = out["dispatch"]
    assert set(d["A"].columns) >= {"nuclear", "gas", "res", "ens", "dump"}
    assert (d[("A", "nuclear")] <= 40000 + 1e-6).all()
    assert (d[("A", "gas")] <= 16000 + 1e-6).all(), "two gas units, 10 GW + 6 GW"
    assert (d[("A", "nuclear")] >= -1e-6).all() and (d[("A", "gas")] >= -1e-6).all()


def test_res_is_the_dispatched_part_not_the_potential(monkeypatch):
    """RES is a curtailable column, so the capture must report what CLEARED. With demand far below the
    must-take potential the LP curtails, and a capture that echoed `res_pot` would miss it."""
    monkeypatch.setenv("DISPATCH_CAPTURE_DISPATCH", "1")
    out, _ = _run(res_a=3000.0)
    assert (out["dispatch"][("A", "res")] <= 3000.0 + 1e-6).all()
    # a potential larger than the zone can absorb or export must not be reported as generated
    big, _ = _run(res_a=90000.0, dA=5000, ntc=1000)
    assert (big["dispatch"][("A", "res")] < 90000.0).any(), "unabsorbable RES was reported as dispatched"


def test_capture_covers_every_zone(monkeypatch):
    monkeypatch.setenv("DISPATCH_CAPTURE_DISPATCH", "1")
    out, T = _run()
    d = out["dispatch"]
    assert set(d.columns.get_level_values("zone")) == {"A", "B"}
    assert list(d.index) == list(T)


@pytest.mark.parametrize("flag", ["0", "false", "False"])
def test_falsy_flag_values_are_off(monkeypatch, flag):
    monkeypatch.setenv("DISPATCH_CAPTURE_DISPATCH", flag)
    out, _ = _run()
    assert "dispatch" not in out
