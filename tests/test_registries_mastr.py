"""MaStR ETL logic that must be right before any data lands (ADR-7).

The two pieces MaStR does *not* hand us and we therefore derive:
  * `derive_scheme` — the support scheme is a **statutory rule** on (commissioning, capacity, auction),
    never a registry column. It sets the negative-price bid floor, so getting it wrong silently
    mis-prices every negative hour.
  * `_reserve_flag` — units in grid reserve / Sicherheitsbereitschaft are in *installed capacity* but
    cannot be dispatched into the market. Counting them inflates the stack and depresses prices.
"""
from __future__ import annotations

import pandas as pd

from pricemodeling.registries.mastr import _reserve_flag, derive_scheme


def _scheme(years, caps_mw, auction=None):
    comm = pd.Series([pd.Timestamp(f"{y}-06-01") if y else pd.NaT for y in years])
    cap = pd.Series(caps_mw, dtype="float")
    auc = pd.Series(auction if auction is not None else [False] * len(years))
    return list(derive_scheme(comm, cap, auc))


def test_pre_2012_is_feed_in_tariff():
    assert _scheme([2005, 2011], [0.05, 2.0]) == ["fit", "fit"]


def test_2012_2015_direct_marketing_above_500kw():
    # 0.4 MW = 400 kW → below the 500 kW threshold ⇒ still FiT; 2 MW ⇒ market premium
    assert _scheme([2013, 2013], [0.4, 2.0]) == ["fit", "market_premium"]


def test_2016_onwards_threshold_drops_to_100kw():
    # 0.4 MW = 400 kW now exceeds the 100 kW threshold ⇒ market premium
    assert _scheme([2017, 2017], [0.4, 0.05]) == ["market_premium", "fit"]


def test_auction_award_forces_market_premium_regardless_of_size():
    assert _scheme([2019], [0.05], auction=[True]) == ["market_premium"]


def test_unknown_commissioning_yields_no_scheme():
    assert pd.isna(_scheme([None], [1.0])[0])


def test_reserve_flag_true_only_when_a_reserve_date_is_set():
    df = pd.DataFrame({
        "NetzreserveAbDatum": [None, "2017-01-01", None, None],
        "SicherheitsbereitschaftAbDatum": [None, None, "2016-10-01", None],
        "DatumUeberfuehrungInReserve": [None, None, None, None],
    })
    assert list(_reserve_flag(df)) == [False, True, True, False]


def test_reserve_flag_tolerates_missing_columns():
    assert list(_reserve_flag(pd.DataFrame({"x": [1, 2]}))) == [False, False]
