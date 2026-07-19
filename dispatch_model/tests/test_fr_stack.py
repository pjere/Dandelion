"""DISP Phase 4 — FR unit-level economic stack + SRMC merit order."""
from __future__ import annotations

import pytest
from dispatch_model.commodities.model import CommodityModel
from dispatch_model.config import load_config
from dispatch_model.stacks.fr_stack import build_fr_stack, srmc


@pytest.fixture(scope="module")
def stack():
    cfg = load_config("config.yaml")
    if not cfg.resolve(cfg.section("data")["sqlite_path"]).exists():
        pytest.skip("pricemodeling DB not present")
    return cfg, build_fr_stack(cfg)


def _month(cm, year):
    pm = cm.monthly_prices(year, year)
    return {c: pm[(pm.commodity == c) & (pm.date.dt.month == 1)].price.iloc[0]
            for c in ["gas", "co2", "coal", "oil"]}


def test_stack_composition(stack):
    _, st = stack
    assert len(st) > 100
    assert 80_000 < st["capacity_mw"].sum() < 105_000              # FR dispatchable ~90 GW
    assert {"nuclear", "gas", "coal", "oil"} <= set(st["tech"])
    thermal = st[st["tech"].isin(["gas", "coal", "oil"])]
    assert thermal["efficiency"].between(0.30, 0.60).all()         # dispersed within class bands


def test_merit_order_and_fuel_switch(stack):
    cfg, st = stack
    cm = CommodityModel()
    s19 = srmc(st, _month(cm, 2019))
    by19 = st.assign(s=s19).groupby("tech")["s"].mean()
    assert by19["nuclear"] < by19["gas"]                           # nuclear baseload cheapest thermal
    assert by19["hydro_ror"] < by19["nuclear"]
    assert 35 < by19["gas"] < 60                                   # 2019 gas plant SRMC
    # 2022 gas shock flips gas above coal (endogenous fuel switching)
    by22 = st.assign(s=srmc(st, _month(cm, 2022))).groupby("tech")["s"].mean()
    assert by22["coal"] < by22["gas"]
    assert by22["gas"] > 250                                       # crisis-level gas SRMC


def test_efficiency_dispersion_gives_slope(stack):
    _, st = stack
    gas = st[st["tech"] == "gas"]["efficiency"]
    assert gas.std() > 0.02                                        # spread → mid-merit curve has slope
