"""DM Phase 2 offline tests: weighting, thermal-inertia smoothing, cloud→GHI."""
from __future__ import annotations

import numpy as np
import pandas as pd
from demand_model.features.irradiance import clearsky_ghi, ghi_from_cloud
from demand_model.features.temperature import heating_cooling, national_temperature, smoothed_temperatures


def _weather(times, temps_by_station, cloud=0.0):
    frames = []
    for sid, temp in temps_by_station.items():
        frames.append(pd.DataFrame({"timestamp_utc": times, "station_id": sid,
                                     "temperature_c": temp, "cloud_cover_pct": cloud}))
    return pd.concat(frames, ignore_index=True)


def test_national_temperature_weighted():
    times = pd.date_range("2020-01-01", periods=5, freq="h", tz="UTC")
    w = _weather(times, {"A": 10.0, "B": 20.0})
    weights = pd.Series({"A": 0.75, "B": 0.25})
    tn = national_temperature(w, weights)
    assert np.allclose(tn.to_numpy(), 12.5)          # 0.75*10 + 0.25*20


def test_smoothing_and_hdd_cdd():
    times = pd.date_range("2020-01-01", periods=24 * 30, freq="h", tz="UTC")
    rng = np.random.default_rng(0)
    t = pd.Series(8 + 5 * np.sin(np.arange(len(times)) / 24) + rng.normal(0, 2, len(times)), index=times)
    sm = smoothed_temperatures(t, [12, 60], [1, 2])
    # smoothed series are less variable than raw; lagged daily means present
    assert sm["T_smooth_60h"].std() < t.std()
    assert {"T_lag_d1", "T_lag_d2"}.issubset(sm.columns)
    hc = heating_cooling(sm["T_smooth_60h"], tau_heat=15, tau_cool=20)
    assert (hc["HDD"] >= 0).all() and (hc["CDD"] >= 0).all()
    assert hc["HDD"].mean() > hc["CDD"].mean()       # January -> heating dominates


def test_cloud_reduces_ghi():
    stations = pd.DataFrame({"station_id": ["P"], "latitude": [48.8], "longitude": [2.35], "altitude": [35]})
    times = pd.date_range("2021-06-21 00:00", periods=24, freq="h", tz="UTC")
    cs = clearsky_ghi(times, stations)
    assert cs["P"].iloc[12] > 300 and cs["P"].iloc[1] == 0     # midday sun, night dark
    cloud = pd.DataFrame({"P": [90.0] * 24}, index=times)
    ov = ghi_from_cloud(cs, cloud)
    assert ov["P"].iloc[12] < cs["P"].iloc[12] * 0.6           # heavy cloud strongly attenuates
