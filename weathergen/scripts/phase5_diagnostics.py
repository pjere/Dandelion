"""Phase 5 acceptance: simulated vs observed inter-station correlation-vs-distance,
cross-variable correlation, and per-variable ACF to 48 h. Saves a figure to reports/.
"""
from __future__ import annotations

import copy
import warnings
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

warnings.filterwarnings("ignore")

import xarray as xr
from scipy.special import ndtri
from weathergen._cube import to_matrix
from weathergen.config import load_config
from weathergen.model import FittedModel
from weathergen.simulate import simulate

from weathergen import climatology, dependence, io, marginals, transforms, trend

cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")

# build the fit chain ONCE (avoids a second QC pass) and keep the observed anomalies
ing = io.build_dataset(cfg, cfg.rng()); obs_cube = ing.station_cube
tset = transforms.fit(obs_cube, cfg.variables); g = tset.forward(obs_cube)
cc = cfg.section("climatology")
spec = climatology.HarmonicSpec(cc["seasonal_harmonics"], cc["diurnal_harmonics"],
                                cc["interact_seasonal_diurnal"], cc["use_local_solar_time"])
clim = climatology.fit(g, spec); obs_anom = clim.standardize(g)
mat, keys = to_matrix(obs_anom)
dry_da = xr.zeros_like(obs_cube, dtype=bool)
for v in map(str, obs_cube["variable"].values):
    vc = cfg.variables[v]
    if vc.get("kind") == "intermittent":
        dry_da.loc[{"variable": v}] = obs_cube.sel(variable=v) <= vc.get("wet_threshold_mm", 0.1)
dry_mat, _ = to_matrix(dry_da)
q = float(cfg.section("marginals")["threshold_quantile"])
margs = marginals.fit(mat, keys, cfg.variables, q, dry_mat)
dcfg = cfg.section("dependence")
dep = dependence.fit(ndtri(margs.to_uniform(mat, dry_mat)), dcfg["eof_variance"],
                     int(dcfg["var_order"]), dcfg.get("copula", "gaussian"), cfg.rng())
model = FittedModel(cfg.raw, ing.station_meta, [str(v) for v in obs_cube["variable"].values],
                    [str(s) for s in obs_cube["station"].values], clim, tset, margs, dep,
                    trend.fit(cfg.section("trend")))

# simulate 3 years and re-derive its anomalies the same way
cfg5 = copy.deepcopy(cfg); cfg5.raw["simulate"]["horizon_years"] = 3
sim_cube = simulate(model, cfg5, cfg.rng())
sim_cube = sim_cube.sel(variable=list(cfg.var_names))            # drop derived humidity
sim_anom = model.climatology.standardize(model.transforms.forward(sim_cube))

meta = model.station_meta.set_index("station_id")
ids = [str(s) for s in obs_anom["station"].values]


def haversine(a, b):
    la1, lo1, la2, lo2 = map(np.radians, [meta.loc[a, "latitude"], meta.loc[a, "longitude"],
                                          meta.loc[b, "latitude"], meta.loc[b, "longitude"]])
    h = np.sin((la2 - la1) / 2)**2 + np.cos(la1) * np.cos(la2) * np.sin((lo2 - lo1) / 2)**2
    return 6371 * 2 * np.arcsin(np.sqrt(h))


def corr_vs_dist(anom, var):
    a = anom.sel(variable=var).values            # (T, S)
    d, c = [], []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            xi, xj = a[:, i], a[:, j]
            ok = ~np.isnan(xi) & ~np.isnan(xj)
            if ok.sum() < 1000:
                continue
            d.append(haversine(ids[i], ids[j])); c.append(np.corrcoef(xi[ok], xj[ok])[0, 1])
    return np.array(d), np.array(c)


def acf(x, nlag):
    x = x[~np.isnan(x)]; x = x - x.mean()
    v = np.dot(x, x)
    return np.array([np.dot(x[:len(x) - k], x[k:]) / v for k in range(nlag + 1)])


fig, ax = plt.subplots(1, 3, figsize=(16, 4.6))
# (1) correlation vs distance (temperature)
do, co = corr_vs_dist(obs_anom, "temperature_c")
ds, cs = corr_vs_dist(sim_anom, "temperature_c")
ax[0].scatter(do, co, s=6, alpha=.3, label="observed")
ax[0].scatter(ds, cs, s=6, alpha=.3, color="r", label="simulated")
ax[0].set_title("temperature anomaly corr vs distance"); ax[0].set_xlabel("great-circle km")
ax[0].set_ylabel("correlation"); ax[0].legend()

# (2) ACF to 48 h (temperature, wind) at a station
si = 0
for v, c in [("temperature_c", "C0"), ("wind_speed_ms", "C1")]:
    ao = acf(obs_anom.sel(variable=v).values[:, si], 48)
    as_ = acf(sim_anom.sel(variable=v).values[:, si], 48)
    ax[1].plot(ao, c, label=f"{v} obs")
    ax[1].plot(as_, c, ls="--", label=f"{v} sim")
ax[1].set_title("ACF to 48 h (S0)"); ax[1].set_xlabel("lag (h)"); ax[1].legend(fontsize=8)

# (3) cross-variable correlation (off-diagonal pairs), obs vs sim, at S0
ao = np.column_stack([obs_anom.sel(variable=v).values[:, si] for v in cfg.var_names])
as_ = np.column_stack([sim_anom.sel(variable=v).values[:, si] for v in cfg.var_names])
mo = np.ma.corrcoef(np.ma.masked_invalid(ao), rowvar=False)
ms = np.ma.corrcoef(np.ma.masked_invalid(as_), rowvar=False)
off = ~np.eye(len(cfg.var_names), dtype=bool)
ax[2].scatter(np.asarray(mo)[off], np.asarray(ms)[off], s=30)
ax[2].plot([-1, 1], [-1, 1], "k:", lw=1)
ax[2].set_xlim(-1, 1); ax[2].set_ylim(-1, 1)
ax[2].set_title("cross-variable corr (S0): obs vs sim"); ax[2].set_xlabel("observed"); ax[2].set_ylabel("simulated")

fig.tight_layout()
out = cfg.reports_dir; out.mkdir(parents=True, exist_ok=True)
fig.savefig(out / "phase5_dependence.png", dpi=110)

print("=== Phase 5 acceptance (real data) ===")
print(f"EOF modes k={model.dependence.n_modes}  VAR order p={model.dependence.order}")
# binned corr-vs-distance error
bins = np.linspace(0, max(do.max(), ds.max()), 12)
bo = [co[(do >= bins[i]) & (do < bins[i + 1])].mean() for i in range(len(bins) - 1)]
bs = [cs[(ds >= bins[i]) & (ds < bins[i + 1])].mean() for i in range(len(bins) - 1)]
print(f"corr-vs-dist binned MAE (temp): {np.nanmean(np.abs(np.array(bo) - np.array(bs))):.3f}")
print(f"cross-var corr MAE (S0): {np.mean(np.abs(np.asarray(mo)[off] - np.asarray(ms)[off])):.3f}")
ao48 = acf(obs_anom.sel(variable='temperature_c').values[:, si], 48)
as48 = acf(sim_anom.sel(variable='temperature_c').values[:, si], 48)
print(f"temp ACF MAE to 48h: {np.mean(np.abs(ao48 - as48)):.3f}")
print("figure ->", out / "phase5_dependence.png")
