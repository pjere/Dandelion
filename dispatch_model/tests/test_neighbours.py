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
    # measured monthly history: Jan-2022 gas was 85.8 (the pre-invasion lull, not the generic winter
    # premium's 145) — the SRMC is ~210; the crisis peak moved to August (gas 240 → SRMC ~450+)
    assert b22["gas"] > 180
    m_aug = {c: cm.monthly_prices(2022, 2022).pipe(
        lambda pm: pm[(pm.commodity == c) & (pm.date.dt.month == 8)].price.iloc[0])
        for c in ["gas", "co2", "coal", "oil"]}
    assert st.assign(s=srmc(st, m_aug)).groupby("tech")["s"].mean()["gas"] > 400


def test_participation_caps_only_clamp_in_years_the_market_revealed_the_fleet(monkeypatch):
    """Revealed participation: the ceiling is the annual p99.9 of observed generation per thermal
    tech — but only in a YEAR that priced high enough to reveal the available fleet. In a cheap year
    the p99.9 measures economic dispatch, not availability (2019 had zero hours >150 EUR/MWh in every
    zone, and clamping to its p99.9 made the model invent 202 scarcity hours), so no clamp is
    returned. The test is year-level so cluster zones without their own price series keep their
    clamp — absent price data is not evidence of a cheap year."""
    import numpy as np
    import pandas as pd
    from dispatch_model.neighbours import blocks
    from dispatch_model.rolling import backtest as bt

    idx = pd.date_range("2025-01-01", periods=5000, freq="h", tz="UTC")
    rng = np.random.default_rng(7)
    gen = pd.concat([
        pd.DataFrame({"timestamp_utc": idx, "tech": "gas", "gen_mw": rng.uniform(0, 10_000, len(idx))}),
        pd.DataFrame({"timestamp_utc": idx, "tech": "oil", "gen_mw": rng.uniform(0, 100, len(idx))}),
    ])
    monkeypatch.setattr(blocks, "load_generation_hist", lambda cfg, year, zones=None: gen)

    class _Cfg:                                   # hashable (lru_cache) config stub
        all_zones = ["DE_LU", "AT_SI"]
    cfg = _Cfg()
    scarce = pd.Series(np.where(np.arange(len(idx)) < 500, 300.0, 50.0), index=idx)   # 500 h > 150
    cheap = pd.Series(np.full(len(idx), 50.0), index=idx)                             # never > 150

    # revealing year: clamp applies, and the cluster zone WITHOUT prices is clamped too
    blocks._year_reveals_fleet.cache_clear()
    monkeypatch.setattr(bt, "_observed_prices",
                        lambda c, y, z: {"DE_LU": scarce, "AT_SI": None})
    assert abs(blocks.participation_caps(cfg, "DE_LU", 2025)["gas"] - 9990) < 30
    assert "oil" not in blocks.participation_caps(cfg, "DE_LU", 2025)   # <300 MW: too thin
    assert blocks.participation_caps(cfg, "AT_SI", 2025)                # cluster zone still clamped

    # cheap year: no zone revealed the fleet → no clamp anywhere
    blocks._year_reveals_fleet.cache_clear()
    monkeypatch.setattr(bt, "_observed_prices", lambda c, y, z: {"DE_LU": cheap, "AT_SI": None})
    assert blocks.participation_caps(cfg, "DE_LU", 2019) == {}
    assert blocks.participation_caps(cfg, "AT_SI", 2019) == {}
