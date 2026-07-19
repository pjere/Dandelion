"""Phase 2 acceptance diagnostics on real station data.

Fits the climatology and checks: (1) fitted mean diurnal+seasonal surface reproduces the
observed one; (2) standardized residuals are ~zero-mean / unit-variance with no leftover
diurnal or seasonal structure in mean or variance. Saves a figure to reports/.
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

from weathergen import climatology, io

cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
ingest = io.build_dataset(cfg, cfg.rng())
cube = ingest.station_cube
cc = cfg.section("climatology")
spec = climatology.HarmonicSpec(cc["seasonal_harmonics"], cc["diurnal_harmonics"],
                                cc["interact_seasonal_diurnal"], cc["use_local_solar_time"])
clim = climatology.fit(cube, spec)
anom = clim.standardize(cube)

time = pd.DatetimeIndex(cube["time"].values)
ids = [str(s) for s in cube["station"].values]

def surface(values, offset, agg="mean"):
    lst = ((time.hour + offset) % 24).astype(int)
    df = pd.DataFrame({"v": values, "month": time.month, "hour": lst})
    s = df.pivot_table("v", "month", "hour", aggfunc=agg)
    return s.reindex(index=range(1, 13), columns=range(24))   # full grid, NaN where empty

print("=== Phase 2 acceptance (real station data) ===")
print(f"cube: {dict(cube.sizes)}  | n_basis={spec.n_basis}")
print(f"{'variable':18s} {'surf_RMSE':>9s} {'z_mean':>7s} {'z_std':>6s} "
      f"{'|hourMean|max':>13s} {'|monMean|max':>12s} {'hourStd[min,max]':>18s}")

focus_v, focus_s = "temperature_c", None
for v in cfg.var_names:
    vi = cfg.var_names.index(v)
    rmse, zmu, zsd, hmax, mmax, smin, smax = [], [], [], [], [], [], []
    for si, sid in enumerate(ids):
        x = cube.values[:, si, vi]
        if np.isfinite(x).sum() < 2 * spec.n_basis:
            continue
        off = clim.offsets[sid]
        mu = clim._mu_sigma(time, si)[0][:, vi]
        obs_s = surface(x, off); fit_s = surface(mu, off)
        rmse.append(np.sqrt(np.nanmean((obs_s.values - fit_s.values) ** 2)))
        z = anom.values[:, si, vi]
        zmu.append(np.nanmean(z)); zsd.append(np.nanstd(z))
        zser = pd.Series(z)
        lst = ((time.hour + off) % 24).astype(int)
        hmax.append(zser.groupby(lst).mean().abs().max())
        mmax.append(zser.groupby(time.month).mean().abs().max())
        hs = zser.groupby(lst).std()
        smin.append(hs.min()); smax.append(hs.max())
        if v == focus_v and focus_s is None:
            focus_s = (si, sid, off, obs_s, fit_s, z, lst)
    print(f"{v:18s} {np.nanmean(rmse):9.3f} {np.nanmean(zmu):7.3f} {np.nanmean(zsd):6.3f} "
          f"{np.nanmean(hmax):13.3f} {np.nanmean(mmax):12.3f} "
          f"[{np.nanmean(smin):.2f},{np.nanmean(smax):.2f}]")

# figure for the focus variable/station
si, sid, off, obs_s, fit_s, z, lst = focus_s
fig, ax = plt.subplots(2, 2, figsize=(12, 8))
im0 = ax[0, 0].imshow(obs_s.values, aspect="auto", origin="lower", cmap="RdYlBu_r")
ax[0, 0].set_title(f"Observed mean {focus_v} (month x LST hour) — {sid}")
ax[0, 0].set_xlabel("LST hour"); ax[0, 0].set_ylabel("month"); fig.colorbar(im0, ax=ax[0, 0])
im1 = ax[0, 1].imshow(fit_s.values, aspect="auto", origin="lower", cmap="RdYlBu_r",
                      vmin=im0.get_clim()[0], vmax=im0.get_clim()[1])
ax[0, 1].set_title("Fitted mu (harmonic)"); ax[0, 1].set_xlabel("LST hour"); fig.colorbar(im1, ax=ax[0, 1])
zser = pd.Series(z)
ax[1, 0].plot(zser.groupby(lst).mean(), "o-", label="mean")
ax[1, 0].plot(zser.groupby(lst).std(), "s-", label="std")
ax[1, 0].axhline(0, color="k", lw=.5); ax[1, 0].axhline(1, color="k", lw=.5, ls=":")
ax[1, 0].set_title("Residual z by LST hour"); ax[1, 0].set_xlabel("LST hour"); ax[1, 0].legend()
t = pd.DatetimeIndex(cube["time"].values)
ax[1, 1].plot(zser.groupby(t.month).mean(), "o-", label="mean")
ax[1, 1].plot(zser.groupby(t.month).std(), "s-", label="std")
ax[1, 1].axhline(0, color="k", lw=.5); ax[1, 1].axhline(1, color="k", lw=.5, ls=":")
ax[1, 1].set_title("Residual z by month"); ax[1, 1].set_xlabel("month"); ax[1, 1].legend()
fig.tight_layout()
out = cfg.reports_dir; out.mkdir(parents=True, exist_ok=True)
fig.savefig(out / "phase2_climatology.png", dpi=110)
print("figure ->", out / "phase2_climatology.png")
