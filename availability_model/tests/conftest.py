"""Session-scoped fixtures: the DB-derived artifacts are built once for the whole test session.

Combined with the on-disk scan cache (io/cache.py), this turns a multi-hour suite into a few minutes.
"""
from __future__ import annotations

import pytest
from availability_model.config import load_config


@pytest.fixture(scope="session")
def cfg():
    c = load_config("config.yaml")
    if not c.resolve(c.section("data")["sqlite_path"]).exists():
        pytest.skip("pricemodeling DB not present")
    return c


@pytest.fixture(scope="session")
def registry(cfg):
    from availability_model.io.fleet import build_fleet_registry
    return build_fleet_registry(cfg)


@pytest.fixture(scope="session")
def events(cfg, registry):
    from availability_model.io.outages import infer_outage_events
    return infer_outage_events(cfg, registry)


@pytest.fixture(scope="session")
def model(cfg):
    from availability_model.pipeline import calibrate
    return calibrate(cfg)
