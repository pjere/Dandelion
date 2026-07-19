"""TYNDP capacity trajectories (#76): interpolation, per-zone factors, RES = wind+solar, CAGR fallback."""
from __future__ import annotations

from dispatch_model.stacks.costs import VOM
from dispatch_model.tyndp import _interp, flex_capacity_mw, tyndp_factors

_TYNDP = {
    "FR": {
        "demand_twh": {2025: 460.0, 2050: 640.0},
        "cap_nuclear_gw": {2025: 61.0, 2050: 40.0},          # nuclear declines
        "cap_gas_gw": {2025: 12.0, 2050: 6.0},
        "cap_wind_gw": {2025: 25.0, 2050: 110.0},
        "cap_solar_gw": {2025: 20.0, 2050: 130.0},           # RES surges
    },
}


def test_interp_clamps_outside_anchor_range():
    s = {2025: 10.0, 2050: 35.0}
    assert _interp(s, 2025) == 10.0
    assert abs(_interp(s, 2037.5) - 22.5) < 1e-6              # midpoint
    assert _interp(s, 2060) == 35.0                          # clamps beyond the last anchor
    assert _interp({}, 2030) is None


def test_factors_demand_res_and_capacity():
    f = tyndp_factors(_TYNDP, "FR", 2050, 2025)
    assert abs(f["demand"] - 640.0 / 460.0) < 1e-6
    # RES volume grows with total wind+solar: (110+130)/(25+20) = 240/45
    assert abs(f["res"] - 240.0 / 45.0) < 1e-6
    assert abs(f["cap"]["nuclear"] - 40.0 / 61.0) < 1e-6     # nuclear declines (<1)
    assert f["cap"]["nuclear"] < 1.0 and f["cap"]["gas"] < 1.0


def test_reference_year_factors_are_unity():
    f = tyndp_factors(_TYNDP, "FR", 2025, 2025)
    assert abs(f["demand"] - 1.0) < 1e-6 and abs(f["res"] - 1.0) < 1e-6
    assert abs(f["cap"]["nuclear"] - 1.0) < 1e-6


def test_missing_zone_returns_none_for_cagr_fallback():
    assert tyndp_factors(_TYNDP, "DE_LU", 2040, 2025) is None


def test_flex_capacity_is_absolute_and_priced_as_peaking():
    t = {"FR": {"cap_flex_gw": {2025: 2.0, 2050: 28.0}}}
    assert flex_capacity_mw(t, "FR", 2025) == 2000.0
    assert abs(flex_capacity_mw(t, "FR", 2037.5) - 15000.0) < 1.0    # interpolated
    assert flex_capacity_mw(t, "DE_LU", 2040) == 0.0                 # absent → no flex (firm+DSR only)
    # flex (battery/DR/H2-peaker) is priced as a peaking backstop (~€180), not baseload
    assert VOM["flex"] > 100 and VOM["flex"] < 300
