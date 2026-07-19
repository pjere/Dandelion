"""Tests for powersim_core — the shared library. Emphasis on the F4 (RNG) and F5 (leap-day) fixes."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from powersim_core import glossary, meta, rng, time_grid, units


# --- glossary -------------------------------------------------------------
def test_canonical_timestamp_renames_aliases():
    for alias in ("ts_utc", "time"):
        df = pd.DataFrame({alias: [1, 2], "x": [3, 4]})
        assert glossary.TIMESTAMP_UTC in glossary.canonical_timestamp(df).columns
    assert glossary.PRICE_EUR_MWH == "price_eur_mwh"          # unit in the name


# --- time grid (F5) -------------------------------------------------------
def test_model_year_is_8760_even_on_leap_years():
    for y in (2019, 2020, 2024):                              # 2020/2024 are leap
        idx = time_grid.model_year_index(y)
        assert len(idx) == 8760                               # Feb-29 dropped by policy
        assert not ((idx.month == 2) & (idx.day == 29)).any()
        assert str(idx.tz) == "UTC"


def test_multi_year_model_index_and_drop():
    idx = time_grid.model_index(2019, 2021)
    assert len(idx) == 3 * 8760
    df = pd.DataFrame({"timestamp_utc": time_grid.hourly_index("2020-02-28", "2020-03-02")})
    assert (pd.DatetimeIndex(time_grid.to_model_grid(df)["timestamp_utc"]).day == 29).sum() == 0


def test_assert_canonical_grid_catches_violations():
    time_grid.assert_canonical_grid(time_grid.model_year_index(2020), expect_8760=True)   # ok
    naive = pd.date_range("2020-01-01", periods=10, freq="h")                              # tz-naive
    with pytest.raises(ValueError):
        time_grid.assert_canonical_grid(naive)
    dup = pd.DatetimeIndex(["2020-01-01 00:00", "2020-01-01 00:00"], tz="UTC")
    with pytest.raises(ValueError):
        time_grid.assert_canonical_grid(dup)


# --- rng (F4) -------------------------------------------------------------
def test_draw_rng_reproducible_and_independent():
    a = rng.draw_rng(42, 0).standard_normal(1000)
    b = rng.draw_rng(42, 0).standard_normal(1000)
    c = rng.draw_rng(42, 1).standard_normal(1000)
    assert np.array_equal(a, b)                               # reproducible given (seed, draw)
    assert not np.array_equal(a, c)                           # independent across draws
    assert abs(np.corrcoef(a, c)[0, 1]) < 0.1                 # streams uncorrelated


def test_substreams_independent_within_draw():
    wind = rng.substream(42, 3, "wind").standard_normal(500)
    solar = rng.substream(42, 3, "solar").standard_normal(500)
    assert not np.array_equal(wind, solar)
    assert np.array_equal(wind, rng.substream(42, 3, "wind").standard_normal(500))   # reproducible


def test_spawn_draws_all_distinct():
    gens = rng.spawn_draws(7, 20)
    firsts = [g.integers(0, 2**31) for g in gens]
    assert len(set(firsts)) == 20                             # no collisions across 20 parallel draws


# --- units ----------------------------------------------------------------
def test_units():
    assert units.annual_twh(pd.Series([1000.0] * 8760)) == pytest.approx(8.76)   # 1 GW × 8760 h
    assert units.celsius_from_kelvin(273.15) == pytest.approx(0.0)
    assert units.brent_usd_bbl_to_eur_mwh_th(80, 1.08) == pytest.approx(80 / 1.7 / 1.08)


# --- meta -----------------------------------------------------------------
def test_run_metadata_keys(tmp_path):
    cfg = tmp_path / "config.yaml"; cfg.write_text("seed: 1")
    md = meta.run_metadata(config_path=cfg, draw_id=3, seed=42, scenario_id="ref")
    assert md["draw_id"] == 3 and md["seed"] == 42 and md["scenario_id"] == "ref"
    assert md["config_hash"] != "absent" and md["workbook_hash"] == "absent"
    assert "powersim_core_version" in md
