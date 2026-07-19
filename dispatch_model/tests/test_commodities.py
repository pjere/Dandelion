"""DISP Phase 2 — commodity-price module."""
from __future__ import annotations

import numpy as np
from dispatch_model.commodities.model import COMMODITIES, CommodityModel


def _gas(df, year):
    g = df[(df["commodity"] == "gas") & (df["date"].dt.year == year)]
    return g["price"]


def test_deterministic_levels_and_seasonality():
    m = CommodityModel()
    df = m.monthly_prices(2019, 2024)
    assert abs(_gas(df, 2022).mean() - 123.0) < 5           # crisis year seeded from public average
    assert abs(_gas(df, 2019).mean() - 13.5) < 2            # cheap-gas year
    # winter gas premium
    g22 = df[(df["commodity"] == "gas") & (df["date"].dt.year == 2022)]
    jan = g22[g22["date"].dt.month == 1]["price"].mean()
    jul = g22[g22["date"].dt.month == 7]["price"].mean()
    assert jan > jul
    assert (df["price"] > 0).all()


def test_co2_rises_to_2046():
    df = CommodityModel().monthly_prices(2024, 2046)
    co2 = df[df["commodity"] == "co2"]
    assert co2[co2["date"].dt.year == 2046]["price"].mean() > co2[co2["date"].dt.year == 2024]["price"].mean()


def test_stochastic_reproducible_and_varies():
    m = CommodityModel()
    a = m.monthly_prices(2027, 2030, draw=3, stochastic=True)
    b = m.monthly_prices(2027, 2030, draw=3, stochastic=True)
    c = m.monthly_prices(2027, 2030, draw=4, stochastic=True)
    assert np.allclose(a["price"].to_numpy(), b["price"].to_numpy())        # deterministic given draw
    assert not np.allclose(a["price"].to_numpy(), c["price"].to_numpy())    # varies across draws
    # stochastic layer is mean-preserving-ish (log-OU mean 0) — within a few % over many months
    det = m.monthly_prices(2027, 2030)
    for cm in COMMODITIES:
        r = a[a.commodity == cm]["price"].mean() / det[det.commodity == cm]["price"].mean()
        assert 0.7 < r < 1.4


def test_workbook_fallback(tmp_path):
    m = CommodityModel.from_workbook(tmp_path / "nope.xlsx")     # missing → defaults
    assert "gas" in m.annual
