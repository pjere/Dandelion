"""`io.area_capacity` — control-area nameplate allocated to bidding zones by generation share.

`entsoe_installed_capacity` has NO rows for any Italian bidding zone. Not a missing ingestion: every IT
zone was requested and ENTSO-E returned `nodata`, because Italy publishes at control-area level while its
market is split into seven bidding zones (verified against the API: `IT` returns 18 technologies / 97.5 GW
for 2024, `IT_NORD` raises NoMatchingDataError). Italy therefore ran entirely on the p99.9-of-generation
fallback, which under-reads energy-limited plant worst — IT-North reservoir 1.54 GW against an allocated
4.18, PSP 2.52 against 5.18.
"""
from __future__ import annotations

import pandas as pd
import pytest

from dispatch_model.io import area_capacity as AC


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("DISPATCH_AREA_CAPACITY", "1")
    AC._CACHE.clear()


class _Cfg:
    """Minimal stand-in — `allocated_capacity` only uses it for the sqlite path and the cache key."""

    def resolve(self, p):
        return p

    def section(self, _):
        return {"sqlite_path": ":memory:"}


def _patch(monkeypatch, cap_rows, gen_rows):
    """Stand in for the two SQL reads, in the order `allocated_capacity` issues them."""
    frames = [pd.DataFrame(cap_rows), pd.DataFrame(gen_rows)]
    seq = iter(frames)
    monkeypatch.setattr(AC.pd, "read_sql", lambda *a, **k: next(seq))
    monkeypatch.setattr(AC, "db_key", lambda c: "test")


def test_country_capacity_is_split_by_each_zone_generation_share(monkeypatch):
    _patch(monkeypatch,
           [{"sub_key": "Fossil Gas", "mw": 10000.0}],
           [{"zone": "IT_NORTH", "sub_key": "Fossil Gas", "mw": 300.0},
            {"zone": "IT_SUD", "sub_key": "Fossil Gas", "mw": 100.0}])
    got = AC.allocated_capacity(_Cfg(), "IT_NORTH", 2024)
    assert got == pytest.approx({"gas": 7500.0}), "IT_NORTH generated 3/4 of the gas, so it gets 3/4 of it"


def test_a_zone_that_never_generated_a_tech_gets_none_of_it(monkeypatch):
    """Deliberate, and the key's main weakness — capacity that exists but never ran is allocated to zero.

    It is also what makes the key geographically right: Italian hard coal is 63 % Sardinian and 30 %
    IT_CNOR, and IT-North really does have almost none. The model's 0.00 GW of northern coal looked like
    a defect and was not one.
    """
    _patch(monkeypatch,
           [{"sub_key": "Fossil Hard coal", "mw": 5580.0}],
           [{"zone": "IT_SARD", "sub_key": "Fossil Hard coal", "mw": 500.0}])
    assert AC.allocated_capacity(_Cfg(), "IT_NORTH", 2024) == {}


def test_shares_across_the_zones_of_one_area_sum_to_the_country_total(monkeypatch):
    cap = [{"sub_key": "Fossil Gas", "mw": 1000.0}]
    gen = [{"zone": "IT_NORTH", "sub_key": "Fossil Gas", "mw": 60.0},
           {"zone": "IT_CNOR", "sub_key": "Fossil Gas", "mw": 40.0}]
    tot = 0.0
    for z in ("IT_NORTH", "IT_CNOR"):
        AC._CACHE.clear()
        _patch(monkeypatch, cap, gen)
        tot += AC.allocated_capacity(_Cfg(), z, 2024).get("gas", 0.0)
    assert tot == pytest.approx(1000.0), "the allocation must conserve the control-area nameplate"


def test_non_italian_zones_are_untouched(monkeypatch):
    monkeypatch.setattr(AC.pd, "read_sql", lambda *a, **k: pytest.fail("must not query for a mapped-out zone"))
    for z in ("FR", "DE_LU", "GB", "CH", "ES"):
        assert AC.allocated_capacity(_Cfg(), z, 2024) == {}


def test_flag_off_returns_nothing(monkeypatch):
    monkeypatch.setenv("DISPATCH_AREA_CAPACITY", "0")
    monkeypatch.setattr(AC.pd, "read_sql", lambda *a, **k: pytest.fail("must not query when disabled"))
    assert AC.allocated_capacity(_Cfg(), "IT_NORTH", 2024) == {}


def test_missing_data_yields_no_allocation_never_a_default(monkeypatch):
    _patch(monkeypatch, [], [])
    assert AC.allocated_capacity(_Cfg(), "IT_NORTH", 2024) == {}
    AC._CACHE.clear()
    _patch(monkeypatch, [{"sub_key": "Fossil Gas", "mw": 10000.0}], [])
    assert AC.allocated_capacity(_Cfg(), "IT_NORTH", 2024) == {}


def test_gb_and_ch_are_excluded_by_design_not_oversight():
    """GB raises NoMatchingDataError at every level (its data comes from Elexon); CH publishes only four
    technologies, which is the same root cause as the Swiss run-of-river gap and is not fixable here."""
    assert "GB" not in AC._CONTROL_AREA and "CH" not in AC._CONTROL_AREA
    assert set(AC._CONTROL_AREA.values()) == {"IT"}
