"""Trend ON vs OFF decadal comparison on real CMIP6 (SSP2-4.5, 2050, MPI-ESM1-2-LR) deltas.
Same seed for both runs, so the difference is purely the imposed climate signal.
"""
from __future__ import annotations

import warnings
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
from weathergen.config import load_config
from weathergen.model import FittedModel
from weathergen.simulate import simulate

from weathergen import trend

cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
model = FittedModel.load(cfg.models_dir / "fitted.json")

# trend OFF
model.trend = trend.fit({**cfg.section("trend"), "enabled": False}, cfg.variables)
sim_off = simulate(model, cfg, cfg.rng())

# trend ON — real MPI deltas for ssp245/2050
deltas = cfg.models_dir / "cmip6_deltas_ssp245_2050_mpi_esm1_2_lr.npz"
tcfg = {**cfg.section("trend"), "enabled": True, "ssp": "ssp245", "target_year": 2050,
        "cmip6_deltas_path": str(deltas)}
model.trend = trend.fit(tcfg, cfg.variables)
sim_on = simulate(model, cfg, cfg.rng())

yr = pd.DatetimeIndex(sim_off["time"].values).year
years = np.arange(yr.min(), yr.max() + 1)


def annual(sim, var, agg="mean"):
    a = sim.sel(variable=var).mean("station").values
    if agg == "mean":
        return np.array([a[yr == y].mean() for y in years])
    return np.array([np.quantile(a[yr == y], 0.99) for y in years])


fig, ax = plt.subplots(1, 2, figsize=(13, 4.6))
ax[0].plot(years, annual(sim_off, "temperature_c"), "o-", label="trend OFF")
ax[0].plot(years, annual(sim_on, "temperature_c"), "s-", color="r", label="trend ON (SSP2-4.5)")
ax[0].set_title("Annual mean temperature — France"); ax[0].set_xlabel("year"); ax[0].set_ylabel("°C"); ax[0].legend()
ax[1].plot(years, annual(sim_off, "temperature_c", "p99"), "o-", label="OFF p99")
ax[1].plot(years, annual(sim_on, "temperature_c", "p99"), "s-", color="r", label="ON p99")
ax[1].set_title("Annual 99th-pct temperature (heat tail)"); ax[1].set_xlabel("year"); ax[1].legend()
fig.tight_layout()
out = cfg.reports_dir; out.mkdir(parents=True, exist_ok=True)
fig.savefig(out / "trend_on_off.png", dpi=110)

d1, d2 = (years[0], years[0] + 4), (years[-5], years[-1])
def decadal(sim, var):
    a = annual(sim, var)
    return a[(years >= d2[0])].mean() - a[(years <= d1[1])].mean()
print("=== Trend ON vs OFF (SSP2-4.5 / 2050 / MPI-ESM1-2-LR) ===")
print(f"decadal temp drift  OFF: {decadal(sim_off,'temperature_c'):+.2f}°C"
      f" | ON: {decadal(sim_on,'temperature_c'):+.2f}°C")
print(f"mean temp   full-run OFF: {annual(sim_off,'temperature_c').mean():.2f}"
      f" | ON: {annual(sim_on,'temperature_c').mean():.2f}")
print(f"p99  temp   late-decade OFF: {annual(sim_off,'temperature_c','p99')[-5:].mean():.2f}"
      f" | ON: {annual(sim_on,'temperature_c','p99')[-5:].mean():.2f}")
print(f"precip mean OFF: {sim_off.sel(variable='precip_1h_mm').mean().item():.4f}"
      f" | ON: {sim_on.sel(variable='precip_1h_mm').mean().item():.4f} mm/h (drying)")
print("figure ->", out / "trend_on_off.png")
