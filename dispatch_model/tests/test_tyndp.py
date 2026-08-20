"""TYNDP capacity trajectories (#76): interpolation, per-zone factors, RES = wind+solar, CAGR fallback."""
from __future__ import annotations

from dispatch_model.stacks.costs import VOM
from dispatch_model.tyndp import _interp, flex_capacity_mw, tyndp_factors, _RES_YIELD

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
    # RES volume grows with wind+solar capacity WEIGHTED BY YIELD (`_RES_YIELD`), not by raw nameplate:
    # (110*0.28 + 130*0.12) / (25*0.28 + 20*0.12) = 46.4 / 9.4
    w, s = _RES_YIELD["cap_wind_gw"], _RES_YIELD["cap_solar_gw"]
    assert abs(f["res"] - (110 * w + 130 * s) / (25 * w + 20 * s)) < 1e-6
    assert abs(f["cap"]["nuclear"] - 40.0 / 61.0) < 1e-6     # nuclear declines (<1)
    assert f["cap"]["nuclear"] < 1.0 and f["cap"]["gas"] < 1.0


def test_res_factor_is_yield_weighted_not_nameplate():
    """A zone that shifts its RES MIX toward solar must scale by less than nameplate suggests.

    The `res` multiplier scales must-take GENERATION, so it has to be a generation ratio. Measured on
    Portugal: nameplate said x1.77 for 2019->2024 where the metered RES fleet actually grew x1.31, because
    PNEC's build-out is solar-dominated and a solar GW yields ~2.3x less than a wind GW. Weighting by
    `_RES_YIELD` gives x1.38. This test pins the property, not the constants — it still passes if the
    yields are re-measured, so long as wind out-yields solar.
    """
    # same nameplate total (20 GW) either side, but the mix swings wind -> solar
    t = {"Z": {"cap_wind_gw": {2020: 10.0, 2030: 2.0}, "cap_solar_gw": {2020: 10.0, 2030: 18.0}}}
    f = tyndp_factors(t, "Z", 2030, 2020)["res"]
    assert f < 1.0, "a shift from wind to solar at constant nameplate must LOWER the yield ratio"
    # the discarded nameplate form would have called this exactly 1.0
    assert abs((2.0 + 18.0) / (10.0 + 10.0) - 1.0) < 1e-12


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
    # flex (battery/DR/H2-peaker) is priced as a peaking backstop, not baseload. The level was raised
    # 180 -> 300 by the workbook owner once the block actually started dispatching (until the NaN fix in
    # commit 236aa7f it never entered the basis, so its price was inert). Asserted as a BAND, not the
    # exact value: what the test protects is that flex sits above every thermal SRMC and below VoLL.
    assert max(VOM[t] for t in ("ccgt", "ocgt", "coal", "lignite", "oil")) < VOM["flex"] <= 500


def test_flex_vom_is_overridable_and_read_per_call(monkeypatch):
    """The flex bid sets 36 % of French hours in 2046, so it needs a sensitivity knob — and that knob must
    be read at CALL time. Captured at import, a sensitivity arm would silently depend on whether the module
    happened to be imported before or after the environment was set."""
    from dispatch_model.stacks.costs import VOM, flex_vom

    monkeypatch.delenv("DISPATCH_FLEX_VOM", raising=False)
    assert flex_vom() == VOM["flex"]
    monkeypatch.setenv("DISPATCH_FLEX_VOM", "180")
    assert flex_vom() == 180.0                     # same process, no re-import
    monkeypatch.setenv("DISPATCH_FLEX_VOM", "42.5")
    assert flex_vom() == 42.5


def test_the_flex_block_carries_the_overridden_bid(monkeypatch):
    """The override has to reach the STACK ROW, not just the helper: `srmc()` reads the `vom` column, so a
    row built before the override is applied would keep the default and the arm would be a no-op."""
    import pandas as pd
    from dispatch_model.rolling.projection import _append_flex

    stack = pd.DataFrame([{"unit_id": "Z_gas", "zone": "Z", "tech": "gas", "capacity_mw": 1000.0,
                           "efficiency": 0.55, "min_gen_frac": 0.0, "vom": 3.0}])
    tyndp = {"Z": {"cap_flex_gw": {2030: 5.0, 2040: 5.0}}}
    monkeypatch.setenv("DISPATCH_FLEX_VOM", "180")
    out = _append_flex(stack, "Z", tyndp, 2040)
    row = out[out["tech"] == "flex"]
    assert len(row) == 1 and float(row["capacity_mw"].iloc[0]) == 5000.0
    assert float(row["vom"].iloc[0]) == 180.0
