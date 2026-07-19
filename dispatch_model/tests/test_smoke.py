"""DISP Phase 0/1 smoke: config + zone logic + meta + ENTSO-E history loaders."""
from __future__ import annotations

import pytest
from dispatch_model.config import load_config
from dispatch_model.meta import run_metadata


def _cfg():
    return load_config("config.yaml")


def _have_db(cfg):
    return cfg.resolve(cfg.section("data")["sqlite_path"]).exists()


def test_config_and_zones():
    cfg = _cfg()
    assert cfg.seed > 0
    # 7 physical footprint zones + DE_REST, the virtual export-sink block (NL+AT+DK+PL+CZ) added for the
    # DE-LU merit order (see STEP_VII_METHODOLOGY §1)
    assert len(cfg.zones) == 8 and "DE_REST" in cfg.zones and cfg.unit_resolved_zone == "FR"
    assert cfg.section("zones")["FR"]["unit_resolved"] is True
    # every border connects two declared zones
    zs = set(cfg.all_zones)
    assert all(a in zs and b in zs for a, b in cfg.borders)
    assert cfg.entsoe_code("IT_NORTH") == "IT_NORD"


def test_single_zone_mode_degrades():
    cfg = _cfg()
    cfg.run.mode = "single_zone"
    assert cfg.zones == ["FR"]
    assert cfg.borders == []                                        # borders become supply curves


def test_metadata_stamps():
    m = run_metadata(_cfg(), draw=2, seed=7)
    assert m["seed"] == 7 and m["draw"] == "2" and m["mode"] == "multi_zone"
    assert m["zones"] and "config_hash" in m


def test_entsoe_loaders():
    cfg = _cfg()
    if not _have_db(cfg):
        pytest.skip("pricemodeling DB not present")
    from dispatch_model.io.entsoe_hist import load_demand_hist, load_flows_hist, load_generation_hist, load_prices
    px = load_prices(cfg, year=2024, zones=["FR"])
    if px.empty:
        pytest.skip("ENTSO-E history not yet backfilled")
    assert {"zone", "price_eur_mwh"} <= set(px.columns)
    assert px["price_eur_mwh"].between(-500, 5000).mean() > 0.95   # sane FR day-ahead range
    ld = load_demand_hist(cfg, year=2024, zones=["FR"])
    assert ld["load_mw"].between(25000, 100000).mean() > 0.9       # FR load band
    gen = load_generation_hist(cfg, year=2024, zones=["FR"])
    assert "nuclear" in set(gen["tech"])                           # PSR mapped to tech classes
    fl = load_flows_hist(cfg, year=2024)
    assert (fl["border"].str.contains(">")).all()
