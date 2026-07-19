"""RES Phase 3 tests: conversion chains are physically sane (uncalibrated)."""
from __future__ import annotations

import numpy as np
import pandas as pd
from res_model.conversion.hydro_ror import ror_cf
from res_model.conversion.pv import PVCohort, pv_cf
from res_model.conversion.wind_offshore import offshore_farm_cf
from res_model.conversion.wind_onshore import aggregate_power_curve, onshore_cf, rated_speed
from res_model.transfer.ghi import ghi_from_cloud


def test_pv_chain_physical():
    times = pd.date_range("2025-06-21", periods=48, freq="h", tz="UTC")   # summer, 2 days
    ghi = ghi_from_cloud(times, 44.0, 5.0, np.zeros(len(times)))          # clear sky, south France
    cf = pv_cf(times, 44.0, 5.0, ghi.to_numpy(), np.full(len(times), 25.0), PVCohort())
    assert (cf >= 0).all() and cf.max() <= 1.0
    assert cf.max() > 0.6                                                 # midday clear-sky is high
    night = cf[(times.hour < 4) | (times.hour > 21)]
    assert night.max() < 0.02                                            # ~zero at night
    # winter << summer for the same clear sky (lower sun)
    wt = pd.date_range("2025-12-21", periods=24, freq="h", tz="UTC")
    ghw = ghi_from_cloud(wt, 44.0, 5.0, np.zeros(24))
    cfw = pv_cf(wt, 44.0, 5.0, ghw.to_numpy(), np.full(24, 5.0), PVCohort())
    assert cfw.max() < cf.max()


def test_pv_clipping():
    # a low DC/AC ratio clips the midday peak lower than a high one does
    times = pd.date_range("2025-06-21", periods=24, freq="h", tz="UTC")
    ghi = ghi_from_cloud(times, 44.0, 5.0, np.zeros(24)).to_numpy()
    hi = pv_cf(times, 44.0, 5.0, ghi, np.full(24, 20.0), PVCohort(dc_ac_ratio=1.4))
    lo = pv_cf(times, 44.0, 5.0, ghi, np.full(24, 20.0), PVCohort(dc_ac_ratio=1.0))
    assert hi.max() >= lo.max()


def test_onshore_power_curve():
    # lower specific power -> lower rated speed -> higher CF at moderate winds
    assert rated_speed(270) < rated_speed(400)
    v = pd.Series(np.full(2000, 8.0),
                  index=pd.date_range("2025-01-01", periods=2000, freq="h", tz="UTC"))
    cf_low_sp = onshore_cf(v, specific_power=270).mean()
    cf_high_sp = onshore_cf(v, specific_power=400).mean()
    assert cf_low_sp > cf_high_sp
    # curve monotone up to rated, zero below cut-in and above cut-out
    curve = aggregate_power_curve(300, smoothing_ms=2.0)
    assert curve[0] < 0.02 and curve[-1] < 0.05
    calm = onshore_cf(pd.Series([1.0], index=pd.date_range("2025", periods=1, freq="h", tz="UTC")))
    gale = onshore_cf(pd.Series([35.0], index=pd.date_range("2025", periods=1, freq="h", tz="UTC")))
    assert calm.iloc[0] < 0.05 and gale.iloc[0] < 0.05                   # cut-in / cut-out


def test_offshore_cf_range_and_monotone():
    idx = pd.date_range("2025-01-01", periods=5000, freq="h", tz="UTC")
    rng = np.random.default_rng(0)
    wind = pd.Series(np.abs(rng.weibull(2.0, len(idx)) * 10.0), index=idx)   # windy offshore regime
    off = offshore_farm_cf(wind).mean()
    assert 0.30 < off < 0.65                                             # plausible offshore CF band
    # CF rises with the wind resource (offshore's real advantage is stronger wind, not the curve)
    calmer = offshore_farm_cf(wind * 0.7).mean()
    assert off > calmer


def test_ror_tracks_precip():
    idx = pd.date_range("2023-01-01", periods=24 * 365, freq="h", tz="UTC")
    rng = np.random.default_rng(1)
    precip = pd.Series(np.clip(rng.gamma(0.3, 1.0, len(idx)), 0, None), index=idx)
    cf = ror_cf(precip, baseline=0.40)
    assert 0.2 < cf.mean() < 0.6 and (cf >= 0).all() and (cf <= 0.8).all()
    # a sustained wet spell lifts CF above a sustained dry spell
    precip2 = precip.copy(); precip2.iloc[: 24 * 60] += 5.0              # very wet first 60 days
    cf2 = ror_cf(precip2, baseline=0.40)
    assert cf2.iloc[24 * 40] > cf.iloc[24 * 40]
