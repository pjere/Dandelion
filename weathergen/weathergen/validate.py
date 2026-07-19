"""Phase 8 — validation suite. Compares simulated vs observed and emits an HTML report
with pass/fail flags. Synthetic weather can look fine and still be wrong, so this is broad:
marginals, diurnal+seasonal cycles, autocorrelation & cross-correlation, inter-station
correlation-vs-distance, tails (return levels + threshold exceedances), and — weighted
heavily — spell/persistence statistics (heatwaves, calm-wind runs, dry/wet spells).

Every check yields a metric + PASS/WARN flag against a documented tolerance. The report also
carries the short-record tail-uncertainty warning until ERA5 record-extension is folded in.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import xarray as xr

from .config import Config

WARNING = (
    "TAIL-UNCERTAINTY: EVT tails are fit on the ~12-yr station record (ERA5 extension still "
    "downloading). Extreme return levels beyond ~decadal scale carry extra uncertainty until "
    "the extended record is folded in."
)


@dataclass
class Diag:
    name: str
    category: str
    detail: str
    passed: bool | None = None
    weight: int = 1
    fig: str | None = None          # embedded base64 png


def _fig_b64(fig) -> str:
    buf = BytesIO(); fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _common_vars(obs: xr.DataArray, sim: xr.DataArray) -> list[str]:
    s = set(map(str, sim["variable"].values))
    return [v for v in map(str, obs["variable"].values) if v in s]


def _deseason(cube: xr.DataArray) -> xr.DataArray:
    """Subtract the (month, hour) mean per station/variable — a model-free deseasonalization."""
    t = pd.DatetimeIndex(cube["time"].values)
    key = pd.MultiIndex.from_arrays([t.month, t.hour])
    out = cube.copy()
    for vi in range(cube.sizes["variable"]):
        for si in range(cube.sizes["station"]):
            x = cube.values[:, si, vi]
            s = pd.Series(x)
            clim = s.groupby(key).transform("mean")
            out.values[:, si, vi] = x - clim.values
    return out


def _acf(x: np.ndarray, nlag: int) -> np.ndarray:
    x = x[~np.isnan(x)]
    x = x - x.mean(); v = np.dot(x, x)
    return np.array([np.dot(x[: len(x) - k], x[k:]) / v for k in range(nlag + 1)])


# --------------------------------------------------------------------------- #
#  Diagnostics
# --------------------------------------------------------------------------- #
def _marginals(obs, sim, vars_, figd) -> list[Diag]:
    n = len(vars_)
    fig, ax = plt.subplots(1, n, figsize=(3 * n, 3))
    diags = []
    for i, v in enumerate(vars_):
        o = obs.sel(variable=v).values.ravel(); o = o[~np.isnan(o)]
        s = sim.sel(variable=v).values.ravel(); s = s[~np.isnan(s)]
        qs = np.linspace(0.01, 0.99, 60)
        qo, qsi = np.quantile(o, qs), np.quantile(s, qs)
        ax[i].plot(qo, qsi, ".", ms=3); lim = [min(qo.min(), qsi.min()), max(qo.max(), qsi.max())]
        ax[i].plot(lim, lim, "k:", lw=1); ax[i].set_title(v, fontsize=8)
        ax[i].set_xlabel("obs q"); ax[i].set_ylabel("sim q")
        mean_err = abs(s.mean() - o.mean()); std_err = abs(s.std() - o.std()) / (o.std() + 1e-9)
        p99_err = abs(np.quantile(s, 0.99) - np.quantile(o, 0.99))
        # intermittent (precip): std is dominated by rare extremes an ~12-yr record undersamples,
        # so judge on mean + p99 + occurrence rather than the harsh std ratio.
        if v == "precip_1h_mm":
            wet_o, wet_s = (o > 0.1).mean(), (s > 0.1).mean()
            passed = (mean_err < 0.1 and p99_err < 0.5 and abs(wet_s - wet_o) < 0.05)
            detail = (f"Δmean={s.mean()-o.mean():+.3f}, Δp99={p99_err:+.2f}, "
                      f"wet {100*wet_o:.1f}%→{100*wet_s:.1f}%")
        else:
            passed = (mean_err < max(0.5, 0.1 * o.std()) and std_err < 0.15)
            detail = (f"Δmean={s.mean()-o.mean():+.2f}, Δstd={100*(s.std()/o.std()-1):+.0f}%, "
                      f"Δp99={np.quantile(s,0.99)-np.quantile(o,0.99):+.2f}")
        diags.append(Diag(f"marginal {v}", "Marginals", detail, passed=passed))
    diags.insert(0, Diag("QQ plots (all variables)", "Marginals", "quantile-quantile obs vs sim",
                         passed=None, fig=_fig_b64(fig)))
    return diags


def _diurnal_seasonal(obs, sim, figd) -> list[Diag]:
    t_o, t_s = pd.DatetimeIndex(obs["time"].values), pd.DatetimeIndex(sim["time"].values)
    o = obs.sel(variable="temperature_c").mean("station").values
    s = sim.sel(variable="temperature_c").mean("station").values
    so = pd.Series(o).groupby([t_o.month, t_o.hour]).mean().unstack()
    ss = pd.Series(s).groupby([t_s.month, t_s.hour]).mean().unstack().reindex(index=so.index, columns=so.columns)
    rmse = np.sqrt(np.nanmean((so.values - ss.values) ** 2))
    fig, ax = plt.subplots(1, 2, figsize=(9, 3.2))
    ax[0].imshow(so.values, aspect="auto", origin="lower", cmap="RdYlBu_r"); ax[0].set_title("obs mean T (month×hour)")
    ax[1].imshow(ss.values, aspect="auto", origin="lower", cmap="RdYlBu_r"); ax[1].set_title("sim")
    return [Diag("diurnal+seasonal surface (T)", "Cycles", f"mean-surface RMSE {rmse:.2f}°C",
                 passed=(rmse < 1.0), fig=_fig_b64(fig))]


def _acf_crosscorr(obs, sim, vars_, figd, oa, sa) -> list[Diag]:
    fig, ax = plt.subplots(1, 2, figsize=(10, 3.4))
    maes = []
    for v, c in [("temperature_c", "C0"), ("wind_speed_ms", "C1")]:
        ao = _acf(oa.sel(variable=v).values[:, 0], 48); as_ = _acf(sa.sel(variable=v).values[:, 0], 48)
        ax[0].plot(ao, c, label=f"{v} obs"); ax[0].plot(as_, c, ls="--", label=f"{v} sim")
        maes.append(np.mean(np.abs(ao - as_)))
    ax[0].set_title("ACF to 48h (S0)"); ax[0].set_xlabel("lag (h)"); ax[0].legend(fontsize=7)
    # cross-variable correlation at S0
    mo = np.ma.corrcoef(np.ma.masked_invalid(
        np.column_stack([oa.sel(variable=v).values[:, 0] for v in vars_])), rowvar=False)
    ms = np.ma.corrcoef(np.ma.masked_invalid(
        np.column_stack([sa.sel(variable=v).values[:, 0] for v in vars_])), rowvar=False)
    off = ~np.eye(len(vars_), dtype=bool)
    cc_mae = float(np.mean(np.abs(np.asarray(mo)[off] - np.asarray(ms)[off])))
    ax[1].scatter(np.asarray(mo)[off], np.asarray(ms)[off], s=25); ax[1].plot([-1, 1], [-1, 1], "k:")
    ax[1].set_title("cross-var corr obs vs sim"); ax[1].set_xlim(-1, 1); ax[1].set_ylim(-1, 1)
    return [Diag("ACF to 48h", "Dependence", f"temp/wind ACF MAE {np.mean(maes):.3f}",
                 passed=(np.mean(maes) < 0.15), weight=1, fig=_fig_b64(fig)),
            Diag("cross-variable correlation", "Dependence", f"off-diagonal MAE {cc_mae:.3f}",
                 passed=(cc_mae < 0.15))]


def _corr_distance(obs, sim, figd, oa, sa) -> list[Diag]:
    lat = obs["latitude"].values; lon = obs["longitude"].values
    def cvd(a):
        A = a.sel(variable="temperature_c").values; d, c = [], []
        for i in range(A.shape[1]):
            for j in range(i + 1, A.shape[1]):
                xi, xj = A[:, i], A[:, j]; ok = ~np.isnan(xi) & ~np.isnan(xj)
                if ok.sum() < 1000:
                    continue
                la1, lo1, la2, lo2 = map(np.radians, [lat[i], lon[i], lat[j], lon[j]])
                h = np.sin((la2-la1)/2)**2 + np.cos(la1)*np.cos(la2)*np.sin((lo2-lo1)/2)**2
                d.append(6371*2*np.arcsin(np.sqrt(h))); c.append(np.corrcoef(xi[ok], xj[ok])[0, 1])
        return np.array(d), np.array(c)
    do, co = cvd(oa); ds, cs = cvd(sa)
    bins = np.linspace(0, max(do.max(), ds.max()), 12)
    bo = np.array([co[(do >= bins[i]) & (do < bins[i+1])].mean() for i in range(11)])
    bs = np.array([cs[(ds >= bins[i]) & (ds < bins[i+1])].mean() for i in range(11)])
    mae = np.nanmean(np.abs(bo - bs))
    fig, ax = plt.subplots(figsize=(5, 3.4))
    ax.scatter(do, co, s=5, alpha=.25, label="obs"); ax.scatter(ds, cs, s=5, alpha=.25, color="r", label="sim")
    ax.set_title("temp corr vs distance"); ax.set_xlabel("km"); ax.set_ylabel("corr"); ax.legend()
    return [Diag("inter-station corr vs distance", "Dependence", f"binned MAE {mae:.3f}",
                 passed=(mae < 0.1), fig=_fig_b64(fig))]


def _tails(obs, sim, figd) -> list[Diag]:
    diags = []; fig, ax = plt.subplots(1, 2, figsize=(10, 3.4))
    for k, (v, thr, unit) in enumerate([("temperature_c", 30.0, "°C"), ("wind_speed_ms", 15.0, "m/s")]):
        o = obs.sel(variable=v).values.ravel(); o = o[~np.isnan(o)]
        s = sim.sel(variable=v).values.ravel(); s = s[~np.isnan(s)]
        ro = np.sort(o)[::-1]; rs = np.sort(s)[::-1]
        ax[k].semilogx(o.size/np.arange(1, o.size+1), ro, ".", ms=2, alpha=.4, label="obs")
        ax[k].semilogx(s.size/np.arange(1, s.size+1), rs, ".", ms=2, alpha=.4, color="r", label="sim")
        ax[k].set_title(f"{v} return levels"); ax[k].set_xlabel("return period (obs)"); ax[k].legend(fontsize=7)
        exo = 100 * (o > thr).mean(); exs = 100 * (s > thr).mean()
        diags.append(Diag(f"exceedance {v}>{thr}{unit}", "Tails",
                          f"obs {exo:.2f}% vs sim {exs:.2f}% of hours",
                          passed=(abs(exo - exs) < max(0.3, 0.5 * exo))))
    diags.insert(0, Diag("return-level plots", "Tails", "obs vs sim return levels (temp, wind)",
                         passed=None, fig=_fig_b64(fig)))
    return diags


def _spell(x: np.ndarray, cond) -> np.ndarray:
    """Lengths of consecutive runs where cond(x) holds."""
    m = cond(x); lengths = []; run = 0
    for b in m:
        if b:
            run += 1
        elif run:
            lengths.append(run); run = 0
    if run:
        lengths.append(run)
    return np.array(lengths) if lengths else np.array([0])


def _spells(obs, sim, figd) -> list[Diag]:
    """Persistence — the part generators most often get wrong; weighted heavily."""
    diags = []
    specs = [
        ("dry spells (precip<0.1mm)", "precip_1h_mm", lambda x: x < 0.1),
        ("calm-wind spells (<2 m/s)", "wind_speed_ms", lambda x: x < 2.0),
        ("heat spells (T>28°C)", "temperature_c", lambda x: x > 28.0),
    ]
    fig, ax = plt.subplots(1, 3, figsize=(13, 3.4))
    for k, (name, v, cond) in enumerate(specs):
        lo = np.concatenate([_spell(obs.sel(variable=v).values[:, si], cond) for si in range(obs.sizes["station"])])
        ls = np.concatenate([_spell(sim.sel(variable=v).values[:, si], cond) for si in range(sim.sizes["station"])])
        lo, ls = lo[lo > 0], ls[ls > 0]
        if lo.size < 5 or ls.size < 5:
            diags.append(Diag(name, "Spells (weighted)", "too few events to score", passed=None, weight=3))
            continue
        mo, ms = np.mean(lo), np.mean(ls); p95o, p95s = np.quantile(lo, 0.95), np.quantile(ls, 0.95)
        ax[k].hist(lo, bins=30, density=True, alpha=.5, label="obs")
        ax[k].hist(ls, bins=30, density=True, alpha=.5, label="sim"); ax[k].set_title(name, fontsize=8)
        ax[k].set_xlabel("run length (h)"); ax[k].legend(fontsize=7); ax[k].set_yscale("log")
        diags.append(Diag(name, "Spells (weighted)",
                          f"mean {mo:.1f}h→{ms:.1f}h | p95 {p95o:.0f}h→{p95s:.0f}h",
                          passed=(abs(ms/mo - 1) < 0.25 and abs(p95s/max(p95o,1) - 1) < 0.35), weight=3))
    diags.insert(0, Diag("spell-length distributions", "Spells (weighted)",
                         "consecutive-run histograms (log density)", passed=None, weight=3, fig=_fig_b64(fig)))
    return diags


# --------------------------------------------------------------------------- #
#  Report assembly
# --------------------------------------------------------------------------- #
def validate(obs: xr.DataArray, sim: xr.DataArray, config: Config) -> Path:
    vars_ = _common_vars(obs, sim)
    figd = config.reports_dir / "figures"; figd.mkdir(parents=True, exist_ok=True)
    diags: list[Diag] = []
    oa, sa = _deseason(obs), _deseason(sim)     # compute once, reuse
    diags += _marginals(obs, sim, vars_, figd)
    diags += _diurnal_seasonal(obs, sim, figd)
    diags += _acf_crosscorr(obs, sim, vars_, figd, oa, sa)
    diags += _corr_distance(obs, sim, figd, oa, sa)
    diags += _tails(obs, sim, figd)
    diags += _spells(obs, sim, figd)

    scored = [d for d in diags if d.passed is not None]
    wpass = sum(d.weight for d in scored if d.passed)
    wtot = sum(d.weight for d in scored)
    html = [
        "<html><head><meta charset='utf-8'><title>weathergen validation</title><style>",
        "body{font-family:sans-serif;margin:2rem;max-width:1100px}h2{border-bottom:1px solid #ddd}",
        ".warn{background:#fff3cd;border:1px solid #e0c060;padding:.8rem;margin:1rem 0}",
        ".pass{color:#137333;font-weight:bold}.warnf{color:#b06000;font-weight:bold}",
        "table{border-collapse:collapse;width:100%}",
        "td,th{border:1px solid #ddd;padding:5px 9px;font-size:13px;text-align:left}",
        "img{max-width:100%;border:1px solid #eee;margin:.4rem 0}</style></head><body>",
        "<h1>weathergen — validation report</h1>",
        f"<p><b>Weighted score: {wpass}/{wtot}</b> checks passed "
        f"(spell/persistence checks weighted ×3). Simulated {sim.sizes['time']//8760} yr vs observed record.</p>",
        f"<div class='warn'>⚠ {WARNING}</div>",
    ]
    for cat in ["Marginals", "Cycles", "Dependence", "Tails", "Spells (weighted)"]:
        html.append(f"<h2>{cat}</h2>")
        rows = ["<table><tr><th>check</th><th>result</th><th>flag</th></tr>"]
        for d in [x for x in diags if x.category == cat]:
            if d.fig:
                html.append(f"<img src='data:image/png;base64,{d.fig}'>")
            if d.passed is not None:
                flag = "<span class='pass'>PASS</span>" if d.passed else "<span class='warnf'>WARN</span>"
                rows.append(f"<tr><td>{d.name}</td><td>{d.detail}</td><td>{flag}</td></tr>")
        rows.append("</table>")
        html.append("".join(rows))
    html.append("</body></html>")
    path = config.reports_dir / "validation_report.html"
    path.write_text("\n".join(html), encoding="utf-8")
    return path
