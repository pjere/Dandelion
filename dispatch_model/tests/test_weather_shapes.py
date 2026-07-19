"""#77 weather-coherent shapes — reduced-form neighbour logic (pure; FR slice needs the demand/RES models)."""
from __future__ import annotations

import numpy as np
import pandas as pd
from dispatch_model.weather_shapes import NeighbourWeatherModel, _design


def test_design_hdd_cdd_and_shape():
    idx = pd.date_range("2040-01-01", periods=48, freq="h", tz="UTC")
    temp = np.linspace(-5, 30, 48)
    X = _design(temp, idx)
    assert X.shape == (48, 8)
    # HDD (col 1) high at cold, 0 at warm; CDD (col 2) 0 at cold, high at hot
    assert X[0, 1] > 15 and X[-1, 1] == 0
    assert X[0, 2] == 0 and X[-1, 2] > 5


def test_neighbour_shape_responds_to_temp_and_res_cf():
    # a synthetic zone: load = 8000 + 300·HDD (cold ⇒ more load); RES follows the FR CF shape
    coef = [8000.0, 300.0, 50.0, 0, 0, 0, 0, 0]
    m = NeighbourWeatherModel(load_coef={"DE_LU": coef},
                              res_beta={"DE_LU": {"mean_res_mw": 5000.0, "mean_load_mw": 12000.0}})
    idx = pd.date_range("2040-01-01", periods=8784, freq="h", tz="UTC")
    fr_temp = pd.Series(10 - 12 * np.cos(2 * np.pi * np.arange(len(idx)) / (24 * 365)), index=idx)  # seasonal
    fr_res_cf = pd.Series(np.abs(np.sin(np.arange(len(idx)) / 50)) + 0.1, index=idx)                # wind-like
    s = m.shape("DE_LU", 2040, fr_temp, fr_res_cf, load_growth=1.1, res_growth=2.0)
    assert {"timestamp_utc", "load_mw", "musttake_res_mw"}.issubset(s.columns)
    # coldest hours draw more load than the warmest
    cold = s.loc[s["timestamp_utc"].dt.month.isin([1, 12]), "load_mw"].mean()
    warm = s.loc[s["timestamp_utc"].dt.month.isin([7, 8]), "load_mw"].mean()
    assert cold > warm
    # RES mean ≈ zone mean_res × res_growth (the CF is unit-mean-normalised inside)
    assert 9000 < s["musttake_res_mw"].mean() < 11000        # 5000 × 2.0
    # unknown zone → None (projection falls back to reference shape)
    assert m.shape("ZZ", 2040, fr_temp, fr_res_cf) is None
