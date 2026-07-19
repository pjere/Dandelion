"""Year-varying RES scheme evolution (projection validity of the bid stack)."""
from __future__ import annotations

import pandas as pd
import pytest
from dispatch_model.scheme_evolution import scheme_shares, trigger_hours


@pytest.fixture
def registry_fixture(tmp_path, monkeypatch):
    """A tiny DE_LU RES fleet in an isolated lake: FiT (2005), market-premium (2015), each 20-yr support."""
    monkeypatch.setenv("POWERSIM_LAKE", str(tmp_path / "lake"))
    from powersim_core import registry
    reg = pd.DataFrame({
        "source": "mastr", "source_id": ["W1", "W2"], "zone": "DE_LU", "tech": "wind_onshore",
        "capacity_mw": [1000.0, 1000.0],
        "commissioning_date": ["2005-01-01", "2015-01-01"],
        "scheme": ["fit", "market_premium"],
        "support_end": ["2025-01-01", "2035-01-01"],
    })
    registry.write(reg, "mastr")
    return {"fit": -60.0, "market_premium": -20.0, "merchant": 0.0}


def test_trigger_schedule_tightens_over_time():
    assert trigger_hours(2019) == 6
    assert trigger_hours(2023) == 4
    assert trigger_hours(2025) == 3
    assert trigger_hours(2026) == 2
    assert trigger_hours(2027) == 1 and trigger_hours(2046) == 1


def test_rolloff_moves_capacity_to_merchant_as_support_ends(registry_fixture):
    floors = registry_fixture
    def shares(y):
        return {t["scheme"]: t["share"] for t in scheme_shares("DE_LU", y, floors)}

    s2019 = shares(2019)
    assert s2019["fit"] == pytest.approx(0.5) and s2019["market_premium"] == pytest.approx(0.5)
    # 2030: the 2005 FiT unit (support_end 2025) has rolled to merchant; the 2015 unit still market-premium
    s2030 = shares(2030)
    assert s2030.get("fit", 0) == 0.0
    assert s2030["market_premium"] == pytest.approx(0.5) and s2030["merchant"] == pytest.approx(0.5)
    # 2040: both rolled off → fully merchant
    s2040 = shares(2040)
    assert s2040["merchant"] == pytest.approx(1.0)


def test_merchant_has_no_trigger_but_subsidised_takes_the_year_schedule(registry_fixture):
    trs = {t["scheme"]: t for t in scheme_shares("DE_LU", 2019, registry_fixture)}
    assert trs["market_premium"]["trigger"] == 6 and trs["market_premium"]["floor"] == -20.0
    trs2030 = {t["scheme"]: t for t in scheme_shares("DE_LU", 2030, registry_fixture)}
    assert trs2030["market_premium"]["trigger"] == 1                # §51 tightened to 1h by 2030
    assert trs2030["merchant"]["trigger"] == 0                      # merchant never triggers


def test_new_build_enters_under_prevailing_scheme(registry_fixture):
    # 2040: existing fleet (2×1 GW) is fully merchant; add 2 GW of CfD new build → 2 GW cfd / 2 GW merchant
    trs = {t["scheme"]: t for t in scheme_shares("DE_LU", 2040, registry_fixture,
                                                 new_build_mw={"cfd": 2000.0})}
    assert trs["cfd"]["share"] == pytest.approx(0.5) and trs["merchant"]["share"] == pytest.approx(0.5)
