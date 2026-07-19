"""Phase 6 acceptance: impose a prescribed (CMIP6-like) quantile delta and show that with
trend OFF the series is stationary, with trend ON the decadal mean AND the upper tail drift by
the prescribed amounts (tail faster than mean), with a smooth year-to-year transition.
"""
from __future__ import annotations

import warnings
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import xarray as xr

warnings.filterwarnings("ignore")
from weathergen.trend import Trend

rng = np.random.default_rng(0)
BASE, TARGET = 2027, 2050
time = pd.date_range(f"{BASE}-01-01", periods=24 * 365 * 24, freq="1h")   # 24 years
doy = time.dayofyear.to_numpy(); hod = time.hour.to_numpy()
temp = (12 + 9 * np.cos(2 * np.pi * (doy - 200) / 365.25)
        + 4 * np.cos(2 * np.pi * (hod - 15) / 24) + rng.normal(0, 3, len(time)))
cube = xr.DataArray(temp[:, None, None], dims=("time", "station", "variable"),
                    coords={"time": time.to_numpy(), "station": ["S0"], "variable": ["temperature_c"]})

# prescribed delta at TARGET: stronger in summer, and stronger in the upper tail (intensification)
q = np.linspace(0.05, 0.95, 19)
deltas = np.zeros((12, q.size))
for m in range(12):
    summer = 1.0 + 0.6 * np.cos(2 * np.pi * (m + 1 - 7) / 12)          # peak in July
    deltas[m] = summer * np.interp(q, [0.05, 0.5, 0.95], [1.5, 2.5, 4.5])
# save an example npz in the CMIP6-deltas format the loader expects
out = Path(__file__).resolve().parents[1] / "reports"; out.mkdir(parents=True, exist_ok=True)
np.savez(out / "example_cmip6_deltas.npz", quantiles=q, temperature_c=deltas)

tr = Trend(enabled=True, baseline_year=BASE, target_year=TARGET, quantiles=q,
           deltas={"temperature_c": deltas}, mode={"temperature_c": "add"}, trend_variability=True)
on = tr.apply(cube).values[:, 0, 0]
off = cube.values[:, 0, 0]
yr = pd.DatetimeIndex(cube["time"].values).year
years = np.arange(BASE, yr.max() + 1)
mean_on = np.array([on[yr == y].mean() for y in years])
mean_off = np.array([off[yr == y].mean() for y in years])
p99_on = np.array([np.quantile(on[yr == y], 0.99) for y in years])
p99_off = np.array([np.quantile(off[yr == y], 0.99) for y in years])

fig, ax = plt.subplots(1, 2, figsize=(13, 4.6))
ax[0].plot(years, mean_off, "o-", label="trend OFF")
ax[0].plot(years, mean_on, "s-", color="r", label="trend ON")
ax[0].set_title("Annual mean temperature"); ax[0].set_xlabel("year"); ax[0].set_ylabel("°C"); ax[0].legend()
ax[1].plot(years, p99_off, "o-", label="trend OFF (p99)")
ax[1].plot(years, p99_on, "s-", color="r", label="trend ON (p99)")
ax[1].set_title("Annual 99th-percentile (upper tail)"); ax[1].set_xlabel("year"); ax[1].legend()
fig.tight_layout(); fig.savefig(out / "phase6_trend.png", dpi=110)

f_late = (2046 - BASE) / (TARGET - BASE)
print("=== Phase 6 acceptance ===")
print(f"trend OFF: mean drift {mean_off[-1]-mean_off[0]:+.2f}°C, "
      f"p99 drift {p99_off[-1]-p99_off[0]:+.2f}°C (≈0 = stationary)")
print(f"trend ON : mean drift {mean_on[-1]-mean_on[0]:+.2f}°C, "
      f"p99 drift {p99_on[-1]-p99_on[0]:+.2f}°C  (tail > mean = intensification)")
print(f"expected @2046 (frac {f_late:.2f}): median≈{2.5*f_late:.2f}, p95≈{4.5*f_late:.2f} (summer-amplified)")
print(f"transition monotone: {bool(np.all(np.diff(mean_on) > -0.1))}")
print("figure ->", out / "phase6_trend.png", "| example deltas npz saved")
