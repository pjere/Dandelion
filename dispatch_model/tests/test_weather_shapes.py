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


def test_neighbour_shape_own_res_temp_response_and_coherence():
    # synthetic zone: load = 8000 + 300·HDD (cold ⇒ more load). RES (#157) uses the zone's OWN shape
    # (here a solar-like midday peak), modulated by the FR draw's anomaly vs the FR climatology.
    coef = [8000.0, 300.0, 50.0, 0, 0, 0, 0, 0]
    fit = pd.date_range("2019-01-01", "2020-01-01", freq="h", tz="UTC")[:-1]
    hh = fit.hour.to_numpy(float)
    own = 1.0 + np.clip(np.sin(np.pi * (hh - 6) / 12), 0, None) * 3.0   # own diurnal (solar) shape…
    own = own / own.mean()                                             # …unit-mean
    clim = np.ones(len(fit))                                           # flat FR climatology baseline
    m = NeighbourWeatherModel(load_coef={"DE_LU": coef},
                              res_beta={"DE_LU": {"mean_res_mw": 5000.0, "mean_load_mw": 12000.0}},
                              res_shape={"DE_LU": own}, fr_res_clim=clim)
    idx = pd.date_range("2040-01-01", periods=8784, freq="h", tz="UTC")
    fr_temp = pd.Series(10 - 12 * np.cos(2 * np.pi * np.arange(len(idx)) / (24 * 365)), index=idx)  # seasonal
    fr_res_cf = pd.Series(np.ones(len(idx)), index=idx)               # flat draw ⇒ RES = own shape × level
    s = m.shape("DE_LU", 2040, fr_temp, fr_res_cf, load_growth=1.1, res_growth=2.0)
    assert {"timestamp_utc", "load_mw", "musttake_res_mw"}.issubset(s.columns)
    # coldest hours draw more load than the warmest
    assert s.loc[s["timestamp_utc"].dt.month.isin([1, 12]), "load_mw"].mean() > \
           s.loc[s["timestamp_utc"].dt.month.isin([7, 8]), "load_mw"].mean()
    # level preserved: RES mean ≈ zone mean_res × res_growth (own shape + draw are both unit-mean)
    assert 9000 < s["musttake_res_mw"].mean() < 11000                 # 5000 × 2.0
    # OWN shape drives the diurnal cycle: midday (its peak) ≫ night — NOT a flat/borrowed shape
    hr = pd.DatetimeIndex(s["timestamp_utc"]).hour
    assert s.loc[hr.isin([11, 12, 13, 14]), "musttake_res_mw"].mean() > \
           2 * s.loc[hr.isin([0, 1, 2, 3]), "musttake_res_mw"].mean()
    # weather-coherence: the draw enters as a SHAPE anomaly vs FR-normal (it is unit-mean-normalised, so a
    # uniform scale is inert). A summer-peaked draw against the flat climatology lifts summer RES above winter.
    season = pd.Series(np.where(pd.DatetimeIndex(idx).month.isin([6, 7, 8]), 2.0, 1.0), index=idx)
    hi = m.shape("DE_LU", 2040, fr_temp, season, res_growth=2.0)
    hm = pd.DatetimeIndex(hi["timestamp_utc"]).month
    assert hi.loc[hm.isin([6, 7, 8]), "musttake_res_mw"].mean() > \
           1.3 * hi.loc[hm.isin([1, 2, 12]), "musttake_res_mw"].mean()
    # unknown zone → None (projection falls back to reference shape)
    assert m.shape("ZZ", 2040, fr_temp, fr_res_cf) is None
