"""wind100 co-generation: fit recovers the conditional structure; append is coherent + reproducible."""
from __future__ import annotations

import numpy as np
import pandas as pd
import xarray as xr
from weathergen.wind100 import VAR_NAME, fit_wind100


def _panels(n=24 * 400, S=6, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2018-01-01", periods=n, freq="h", tz="UTC")
    w10 = pd.DataFrame({f"S{j}": np.abs(rng.weibull(2.0, n) * 4.0) + 0.3 for j in range(S)}, index=idx)
    # w100 = 1.5 · w10^0.9 · exp(AR(1) residual shared across stations)
    resid = np.zeros((n, S)); e = rng.normal(0, 0.3, (n, S))
    for t in range(1, n):
        resid[t] = 0.85 * resid[t - 1] + e[t]
    w100 = 1.5 * w10.to_numpy() ** 0.9 * np.exp(resid)
    return w10, pd.DataFrame(w100, index=idx, columns=w10.columns)


def test_fit_recovers_structure():
    w10, w100 = _panels()
    m = fit_wind100(w10, w100)
    assert 0.7 < m.b.mean() < 1.1                       # recovers the ~0.9 log-slope
    assert 0.7 < m.phi.mean() < 0.95                    # recovers the ~0.85 residual persistence
    assert m.sigma.mean() > 0.2                         # substantial conditional variance retained


def _cube(w10: pd.DataFrame, temp: pd.DataFrame | None = None) -> xr.DataArray:
    variables, layers = ["wind_speed_ms"], [w10.to_numpy()]
    if temp is not None:
        variables.append("temperature_c"); layers.append(temp.to_numpy())
    arr = np.stack(layers, axis=-1)
    return xr.DataArray(arr, dims=("time", "station", "variable"),
                        coords={"time": w10.index, "station": list(w10.columns), "variable": variables})


def test_temperature_term_deseasonalized_recovered_and_applied():
    # temperature with a STRONG seasonal cycle + within-season noise; w100 couples to the within-season
    # anomaly (the noise), c>0 (mild winter day = Atlantic storm = windy). The fit must deseasonalize to
    # recover it — a raw-temperature fit would be confounded by the seasonal cycle.
    w10, w100base = _panels()
    rng = np.random.default_rng(5)
    n, S = w10.shape
    doy = w10.index.dayofyear.to_numpy(float)
    seasonal = 12 + 10 * np.sin(2 * np.pi * doy / 365.25)          # ±10 °C annual swing
    noise = rng.normal(0, 3, (n, S))                               # within-season variability
    temp = pd.DataFrame(seasonal[:, None] + noise, index=w10.index, columns=w10.columns)
    c_true = 0.3
    w100 = w100base * np.exp(c_true * (noise / noise.std()))
    m = fit_wind100(w10, w100, temp_panel=temp)
    assert m.c.mean() > 0.2 and abs(m.c.mean() - c_true) < 0.1     # positive, recovered despite the season cycle
    # append reproduces the within-season coupling
    out = m.append(_cube(w10, temp), np.random.default_rng(1))
    w100v = np.nanmean(out.sel(variable=VAR_NAME).values, 1)
    assert np.corrcoef((noise / noise.std()).mean(1), w100v)[0, 1] > 0.15
    # graceful fallback when the cube has no temperature
    out2 = m.append(_cube(w10), np.random.default_rng(1))
    assert VAR_NAME in set(map(str, out2["variable"].values))


def test_append_coherent_and_reproducible():
    w10, w100 = _panels()
    m = fit_wind100(w10, w100)
    cube = _cube(w10)
    a = m.append(cube, np.random.default_rng(1))
    b = m.append(cube, np.random.default_rng(1))
    c = m.append(cube, np.random.default_rng(2))
    va = a.sel(variable=VAR_NAME).values
    assert VAR_NAME in set(map(str, a["variable"].values))
    assert np.allclose(va, b.sel(variable=VAR_NAME).values)              # same seed → identical
    assert not np.allclose(va, c.sel(variable=VAR_NAME).values)          # different seed → different
    # 100 m exceeds 10 m (shear) and stays positively coherent with the 10 m field
    w10v = a.sel(variable="wind_speed_ms").values
    assert np.nanmean(va) > 1.2 * np.nanmean(w10v)
    corr = np.corrcoef(np.nanmean(va, 1), np.nanmean(w10v, 1))[0, 1]
    assert corr > 0.4
    # residual persistence carried into the output (realistic ramps)
    s = np.nanmean(va, 1) - np.nanmean(va)
    assert np.corrcoef(s[:-1], s[1:])[0, 1] > 0.4       # residual AR carried through (diluted by white w10)
