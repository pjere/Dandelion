"""DISP Phase 3 — neighbour aggregated stacks + net load (backtest mode, ENTSO-E actuals)."""
from __future__ import annotations

import pytest
from dispatch_model.commodities.model import CommodityModel
from dispatch_model.config import load_config
from dispatch_model.neighbours.blocks import build_neighbour_stack, neighbour_netload
from dispatch_model.stacks.fr_stack import srmc


@pytest.fixture(scope="module")
def cfg():
    c = load_config("config.yaml")
    if not c.resolve(c.section("data")["sqlite_path"]).exists():
        pytest.skip("pricemodeling DB not present")
    if load_demand_or_skip(c):
        pytest.skip("ENTSO-E neighbour history not backfilled")
    return c


def load_demand_or_skip(c):
    from dispatch_model.io.entsoe_hist import load_demand_hist
    return load_demand_hist(c, year=2019, zones=["DE_LU"]).empty


def test_german_stack_composition(cfg):
    st = build_neighbour_stack(cfg, "DE_LU", 2019)
    caps = st.groupby("tech")["capacity_mw"].sum() / 1000
    assert 60 < caps.sum() < 110                                  # DE dispatchable ~97 GW (installed × avail)
    assert {"nuclear", "lignite", "coal", "gas"} <= set(caps.index)   # German fleet
    assert caps["lignite"] > 8 and caps["coal"] > 8               # lignite + hard coal both large


def test_netload_range(cfg):
    nl = neighbour_netload(cfg, "DE_LU", 2019)
    assert len(nl) > 8000
    assert 10 < nl["netload_mw"].mean() / 1000 < 55               # DE net load band (GW)
    assert (nl["netload_mw"] < nl["load_mw"] + 1).all()           # RES only reduces net load


def test_german_fuel_switch_2022(cfg):
    st = build_neighbour_stack(cfg, "DE_LU", 2022)
    cm = CommodityModel()

    def by_tech(year):
        pm = cm.monthly_prices(year, year)
        m = {c: pm[(pm.commodity == c) & (pm.date.dt.month == 1)].price.iloc[0]
             for c in ["gas", "co2", "coal", "oil"]}
        return st.assign(s=srmc(st, m)).groupby("tech")["s"].mean()

    b19, b22 = by_tech(2019), by_tech(2022)
    assert abs(b19["coal"] - b19["gas"]) < 12                     # 2019: coal≈gas (close competition)
    assert b22["coal"] < b22["gas"] and b22["lignite"] < b22["gas"]   # 2022 gas shock → switch to coal/lignite
    assert b22["gas"] > 250
