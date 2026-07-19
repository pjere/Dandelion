"""Step-(vii) prerequisites: the two *structural* gaps fixed in step vi before any markup is fitted.

Both exist so the step-(vii) markup is not silently absorbing model error:
  A. per-zone gas hub basis  — IT-North/ES burn PSV/MIBGAS, not TTF (was a missing input → −19 % SMC)
  B. neighbour must-run      — thermal blocks could turn down to zero, so foreign zones could never be
                               pushed to the RES bid (model 0 negative hours vs 210 observed in DE-LU)
"""
from __future__ import annotations

import pytest
from dispatch_model.commodities.model import load_zone_basis, zone_prices
from dispatch_model.config import load_config
from dispatch_model.neighbours.blocks import build_neighbour_stack, must_run_frac
from dispatch_model.stacks.costs import thermal_srmc


def _cfg():
    return load_config("config.yaml")


def _workbook(cfg):
    return cfg.resolve(cfg.section("assumptions")["workbook"])


# --- A. zone gas basis ------------------------------------------------------
def test_zone_basis_loads_and_ttf_zones_are_zero():
    basis = load_zone_basis(_workbook(_cfg()))
    assert basis["IT_NORTH"] > 0 and basis["ES"] > 0        # PSV / MIBGAS price above TTF
    assert basis["FR"] == 0.0 and basis["DE_LU"] == 0.0     # TTF reference hubs


def test_zone_prices_only_shifts_gas():
    prices = {"gas": 20.0, "co2": 25.0, "coal": 8.0, "oil": 60.0}
    basis = {"IT_NORTH": 3.0, "FR": 0.0}
    it = zone_prices(prices, "IT_NORTH", basis)
    assert it["gas"] == 23.0
    assert {k: it[k] for k in ("co2", "coal", "oil")} == {"co2": 25.0, "coal": 8.0, "oil": 60.0}
    assert zone_prices(prices, "FR", basis) is prices       # no basis → untouched object
    assert zone_prices(prices, "UNKNOWN", basis) is prices  # unknown zone → no shift


def test_gas_basis_raises_it_srmc_above_fr():
    prices = {"gas": 20.0, "co2": 25.0, "coal": 8.0, "oil": 60.0}
    basis = load_zone_basis(_workbook(_cfg()))
    fr = thermal_srmc("gas", 0.55, zone_prices(prices, "FR", basis))
    it = thermal_srmc("gas", 0.55, zone_prices(prices, "IT_NORTH", basis))
    assert it > fr
    assert it - fr == pytest.approx(basis["IT_NORTH"] / 0.55)   # basis passes through the efficiency


# --- B. neighbour must-run --------------------------------------------------
def test_must_run_fracs_are_ordered_and_bounded():
    cfg = _cfg()
    lig = must_run_frac(cfg, "DE_LU", "lignite")
    gas = must_run_frac(cfg, "DE_LU", "gas")
    oil = must_run_frac(cfg, "DE_LU", "oil")
    assert 0.0 <= oil < gas < lig <= 1.0                    # peakers flexible, lignite most inflexible
    assert must_run_frac(cfg, "DE_LU", "nonexistent_tech") == 0.0


def test_neighbour_thermal_blocks_carry_must_run_floor():
    """Regression for the 0-negative-hours bug: thermal blocks used to be hard-coded min_gen_frac=0."""
    st = build_neighbour_stack(_cfg(), "DE_LU", 2019)
    lignite = st[st["tech"] == "lignite"]
    assert not lignite.empty
    assert (lignite["min_gen_frac"] > 0).all()
    # sub-blocks sum to the tech capacity, so a common floor ⇒ forced = frac × cap
    forced = (lignite["capacity_mw"] * lignite["min_gen_frac"]).sum()
    assert forced > 0.4 * lignite["capacity_mw"].sum()
    # and the whole zone carries a plausible must-run core (DE ~20-25 GW)
    total_forced = (st["capacity_mw"] * st["min_gen_frac"]).sum()
    assert 10_000 < total_forced < 40_000
