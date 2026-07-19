"""Tests for the Parquet lake + DuckDB catalog (§6). Isolated via POWERSIM_LAKE (a tmp dir)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def isolated_lake(tmp_path, monkeypatch):
    monkeypatch.setenv("POWERSIM_LAKE", str(tmp_path / "lake"))
    monkeypatch.setenv("POWERSIM_DUCKDB", str(tmp_path / "powersim.duckdb"))
    from powersim_core import lake
    return lake


def _frame(n=48, start="2020-01-01"):
    idx = pd.date_range(start, periods=n, freq="h", tz="UTC")
    return pd.DataFrame({"timestamp_utc": idx, "load_mw": np.arange(n, dtype=float)})


def test_write_read_roundtrip_and_order_preserved(isolated_lake):
    lake = isolated_lake
    df = _frame().iloc[::-1]                                # reversed → order must be preserved (no sort)
    p = lake.write_table(df, "demand", "projection_hourly", index=False, scenario="reference")
    assert p.name == "part.parquet" and "scenario=reference" in str(p)
    back = lake.read_table("demand", "projection_hourly", scenario="reference")
    pd.testing.assert_frame_equal(back, df.reset_index(drop=True))


def test_read_concatenates_partitions(isolated_lake):
    lake = isolated_lake
    for r in (0, 1, 2):
        lake.write_table(_frame(n=10), "res", "production", index=False, scenario="ref", realization=r)
    allparts = lake.read_table("res", "production")
    assert len(allparts) == 30 and lake.exists("res", "production")
    assert not lake.exists("res", "production", scenario="ref", realization=9)


def test_catalog_views_and_partition_projection(isolated_lake):
    pytest.importorskip("duckdb")
    lake = isolated_lake
    lake.write_table(_frame(n=24), "dispatch", "backtest_prices", index=False, year=2019)
    lake.write_table(_frame(n=12), "dispatch", "backtest_prices", index=False, year=2022)
    from powersim_core import catalog
    catalog.build_catalog()
    cat = catalog.query("SELECT dataset, n_rows FROM _catalog WHERE layer='dispatch'")
    assert int(cat["n_rows"].iloc[0]) == 36                # 24 + 12 across both year partitions
    by_year = catalog.query("SELECT year, count(*) n FROM dispatch__backtest_prices GROUP BY year ORDER BY year")
    assert list(by_year["year"]) == [2019, 2022] and list(by_year["n"]) == [24, 12]
