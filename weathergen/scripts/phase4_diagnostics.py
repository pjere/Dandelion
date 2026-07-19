"""Phase 4 acceptance: spliced-CDF round-trip + monotonicity, GPD return-level extrapolation
vs empirical, and precip censored-marginal occurrence. Saves a figure to reports/.
"""
from __future__ import annotations

import warnings
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

warnings.filterwarnings("ignore")

from weathergen._cube import to_matrix
from weathergen.config import load_config

from weathergen import climatology, io, marginals, transforms

cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
ing = io.build_dataset(cfg, cfg.rng()); cube = ing.station_cube
tset = transforms.fit(cube, cfg.variables); g = tset.forward(cube)
cc = cfg.section("climatology")
spec = climatology.HarmonicSpec(cc["seasonal_harmonics"], cc["diurnal_harmonics"],
                                cc["interact_seasonal_diurnal"], cc["use_local_solar_time"])
clim = climatology.fit(g, spec); anom = clim.standardize(g)
mat, keys = to_matrix(anom)
import xarray as xr

dry_da = xr.zeros_like(cube, dtype=bool)
for v in map(str, cube["variable"].values):
    vc = cfg.variables[v]
    if vc.get("kind") == "intermittent":
        dry_da.loc[{"variable": v}] = cube.sel(variable=v) <= vc.get("wet_threshold_mm", 0.1)
dry_mat, _ = to_matrix(dry_da)
q = float(cfg.section("marginals")["threshold_quantile"])
ms = marginals.fit(mat, keys, cfg.variables, q, dry_mat)


def col_of(station_idx, var):
    vi = cfg.var_names.index(var)
    V = cube.sizes["variable"]
    return station_idx * V + vi


print("=== Phase 4 acceptance (real data) ===")
print(f"{'station/var':24s} {'rt_err':>8s} {'monotone':>8s} {'sampleMax':>9s} {'q1e-4':>8s} {'xi_up':>7s}")
fig, ax = plt.subplots(1, 3, figsize=(16, 4.5))
focus = [(0, "temperature_c"), (0, "wind_speed_ms"), (0, "pressure_sea_hpa")]
for k, (si, v) in enumerate(focus):
    j = col_of(si, v)
    m = ms.cols[j]
    z = mat[:, j]; z = z[~np.isnan(z)]
    # round-trip + monotonicity
    xs = np.linspace(np.nanpercentile(z, 0.1), np.nanpercentile(z, 99.9), 500)
    rt = np.max(np.abs(m.ppf(m.cdf(xs)) - xs))
    mono = bool(np.all(np.diff(m.cdf(np.sort(xs))) >= -1e-12))
    far = m.ppf(np.array([1 - 1e-4]))[0]
    xi = m.c_up if m.f_up is not None else np.nan
    print(f"{f'S{si}/' + v:24s} {rt:8.1e} {str(mono):>8s} {z.max():9.2f} {far:8.2f} {xi:7.2f}")
    # return-level plot (upper): empirical vs fitted
    zs = np.sort(z)[::-1]
    T_emp = z.size / (np.arange(1, z.size + 1))
    T = np.logspace(0.2, 5, 200)
    lvl = m.ppf(1 - 1 / T)
    ax[k].semilogx(T_emp, zs, ".", ms=2, alpha=.4, label="empirical")
    ax[k].semilogx(T, lvl, "r-", lw=2, label="fitted (body+GPD)")
    ax[k].axhline(z.max(), color="grey", ls=":", label="sample max")
    ax[k].set_title(f"{v} return levels (S{si})"); ax[k].set_xlabel("return period (obs)")
    ax[k].set_ylabel("standardized anomaly z"); ax[k].legend(fontsize=8)
fig.tight_layout()
out = cfg.reports_dir; out.mkdir(parents=True, exist_ok=True)
fig.savefig(out / "phase4_marginals.png", dpi=110)

# precip censored marginal summary
pj = col_of(0, "precip_1h_mm")
cens = ms.cols[pj]
print(f"\nprecip (S0): p_dry={cens.p_dry:.3f}  wet upper xi={cens.wet.c_up:.2f}  "
      f"wet 1e-4 level (z)={cens.wet.ppf(np.array([1-1e-4]))[0]:.2f}")
print("figure ->", out / "phase4_marginals.png")
