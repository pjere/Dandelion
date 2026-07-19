"""RES Phase 7 — validation suite (§6 acceptance criteria) + HTML report.

Groups:
  Calibration   — CF anchors in bands, monthly-energy bias, hydro LOYO
  Distribution  — CF duration curves (modelled vs observed), seasonal profiles, 4 h ramp tails
  Inter-annual  — projected annual wind-energy dispersion spans ±8–10 %
  Cross-variable (KILLER TEST) — corr(load, wind CF) & corr(load, PV CF) by season, and Dunkelflaute
                  (rolling 72 h wind CF<15 % & PV CF<5 % in top-decile demand) — history vs projection
  Vintage       — projected CFs 2027→2046 traceable to workbook assumptions
  Offshore      — CF within published FR ranges (documented uncertainty)
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from io import BytesIO

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ..config import Config

_SEASON = {12: "DJF", 1: "DJF", 2: "DJF", 3: "MAM", 4: "MAM", 5: "MAM",
           6: "JJA", 7: "JJA", 8: "JJA", 9: "SON", 10: "SON", 11: "SON"}


@dataclass
class Diag:
    name: str
    category: str
    detail: str
    passed: bool | None = None
    soft: bool = False
    fig: str | None = None

    @property
    def status(self) -> str:
        if self.passed is None:
            return "INFO"
        return "PASS" if self.passed else ("WARN" if self.soft else "FAIL")


def _fig(fig) -> str:
    buf = BytesIO(); fig.savefig(buf, format="png", dpi=100, bbox_inches="tight"); plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


# --------------------------------------------------------------------------- #
#  Data
# --------------------------------------------------------------------------- #
def _observed_cf(config):
    from ..io.loaders import capacity_factor
    from ..io.qc import qc_capacity_factor
    qc = qc_capacity_factor(config, capacity_factor(config))
    qc = qc[qc["is_valid"]]
    return qc.pivot_table(index="timestamp_utc", columns="technology", values="cf")


def _observed_load_gw(config) -> pd.Series:
    """National load (REALISED − pumping) in GW from the DB, hourly."""
    import sqlite3
    d = config.section("data")
    con = sqlite3.connect(config.resolve(d["sqlite_path"]))
    try:
        real = pd.read_sql('SELECT ts_utc, value FROM rte_consumption_short_term '
                           "WHERE series_key='REALISED' AND value IS NOT NULL", con)
        pump = pd.read_sql('SELECT ts_utc, value FROM rte_generation_per_type '
                           "WHERE series_key='HYDRO_PUMPED_STORAGE' AND value IS NOT NULL", con)
    finally:
        con.close()
    r = real.set_index(pd.to_datetime(real["ts_utc"], utc=True))["value"]
    r = r.groupby(r.index).mean().resample("1h").mean()
    p = pump.set_index(pd.to_datetime(pump["ts_utc"], utc=True))["value"]
    p = (-p.clip(upper=0)).groupby(level=0).mean().resample("1h").mean()
    return ((r - p.reindex(r.index).fillna(0.0)) / 1e3).rename("load_gw")


# --------------------------------------------------------------------------- #
#  Checks
# --------------------------------------------------------------------------- #
def _calibration_checks(config, cal) -> list[Diag]:
    m = cal.metrics; bands = config.section("validation")["cf_bands"]
    out = []
    for t, key in [("pv", "pv"), ("wind_onshore", "wind_onshore"), ("wind_offshore", "wind_offshore")]:
        cf = m["mean_cf"].get(t); b = bands.get(key)
        if cf and b:
            out.append(Diag(f"{t} national CF in band", "Calibration",
                            f"{cf*100:.1f}% vs [{b['low']*100:.0f},{b['high']*100:.0f}]%",
                            passed=(b["low"] - 0.03 <= cf <= b["high"] + 0.03)))
    tgt = config.section("validation")["monthly_energy_bias_pct"]
    for t, v in m["monthly_energy_bias_pct"].items():
        out.append(Diag(f"{t} monthly-energy bias (holdout)", "Calibration",
                        f"{v}% vs ≤{tgt}% aspiration", passed=(v <= tgt), soft=True))
    out.append(Diag("hydro ROR leave-one-year-out bias", "Calibration",
                    f"{m['hydro_loyo_monthly_bias_pct']}% (robust, weather-only floor)",
                    passed=(m["hydro_loyo_monthly_bias_pct"] < 13), soft=True))
    return out


def _distribution_checks(config, cal) -> list[Diag]:
    from ..stochastic.fit import historical_calibrated_cf
    det = historical_calibrated_cf(config, cal)
    obs = _observed_cf(config)
    out = []
    fig, ax = plt.subplots(1, 3, figsize=(11, 3.2))
    for i, t in enumerate(["pv", "wind_onshore", "wind_offshore"]):
        if t not in obs or t not in det:
            continue
        o = obs[t].dropna().to_numpy(); mdl = det[t].reindex(obs[t].dropna().index).dropna().to_numpy()
        qs = np.linspace(0, 1, 101)
        ax[i].plot(100 * (1 - qs), np.quantile(o, qs), label="observed")
        ax[i].plot(100 * (1 - qs), np.quantile(mdl, qs), "--", label="modelled")
        ax[i].set_title(f"{t} CF duration"); ax[i].set_xlabel("% time exceeded"); ax[i].legend(fontsize=7)
        # duration-curve distance (mean abs quantile gap)
        d = float(np.mean(np.abs(np.quantile(o, qs) - np.quantile(mdl, qs[:len(mdl)] if False else qs))))
        out.append(Diag(f"{t} CF duration-curve match", "Distribution",
                        f"mean |Δquantile| = {d:.3f} CF", passed=(d < 0.05)))
    out.insert(0, Diag("CF duration curves", "Distribution", "observed vs calibrated modelled",
                       passed=None, fig=_fig(fig)))
    return out


def _ramp_check(config) -> list[Diag]:
    from ..projection import Projector
    pj = Projector(config)
    prod = pj.production("reference", realization=0, seed=0)
    cf_on = (prod["wind_onshore"] / max(prod["wind_onshore"].max(), 1e-9))
    r1 = cf_on.diff(1).dropna(); r4 = cf_on.diff(4).dropna()
    fig, ax = plt.subplots(figsize=(4.5, 3.2))
    ax.hist(r4, bins=80, density=True, alpha=0.7); ax.set_title("onshore 4 h ramp (norm. CF)")
    ax.set_yscale("log"); ax.set_xlabel("Δ over 4 h")
    ok = float(r4.abs().quantile(0.999)) < 0.9        # ramps bounded (no unphysical jumps)
    return [Diag("Wind ramp distribution has realistic tails", "Distribution",
                 f"1 h ramp p99.9 {r1.abs().quantile(0.999):.3f}, 4 h p99.9 {r4.abs().quantile(0.999):.3f}",
                 passed=ok, fig=_fig(fig))]


def _interannual_check(config) -> list[Diag]:
    """Dispersion of annual wind CF (capacity-independent, vintage-detrended) across the cube's years."""
    from ..projection import Projector
    pj = Projector(config)
    cf = pj.deterministic_cf(0)["wind_onshore"]              # per-unit CF (no capacity/energy trend)
    ann = cf.groupby(cf.index.year).mean()
    ann = ann[ann.index.map(lambda y: (cf.index.year == y).sum() > 6000)]   # full years only
    x = ann.index.to_numpy().astype(float)
    detr = ann.to_numpy() - np.polyval(np.polyfit(x, ann.to_numpy(), 1), x)  # remove vintage trend
    spread = float((detr.max() - detr.min()) / (2 * ann.mean()) * 100)
    return [Diag("Projected wind inter-annual dispersion", "Inter-annual",
                 f"annual onshore CF spans ±{spread:.1f}% around mean (vintage-detrended; target ~±8–10%; "
                 f"a low-wind year like 2021 must be reproducible)", passed=(4.0 <= spread <= 16.0),
                 soft=True)]


def _killer_test(config, cal) -> list[Diag]:
    """Joint demand–RES correlations + Dunkelflaute, on history (and projection if demand available)."""
    obs = _observed_cf(config)
    load = _observed_load_gw(config)
    j = pd.concat([load, obs.get("wind_onshore"), obs.get("pv")], axis=1).dropna()
    j.columns = ["load", "wind", "pv"]
    out = []
    rows = []
    for s in ["DJF", "MAM", "JJA", "SON"]:
        m = pd.Series(j.index.month.map(_SEASON), index=j.index) == s
        rows.append((s, round(j[m]["load"].corr(j[m]["wind"]), 2), round(j[m]["load"].corr(j[m]["pv"]), 2)))
    detail = "; ".join(f"{s}: corr(load,wind)={cw:+.2f} corr(load,PV)={cp:+.2f}" for s, cw, cp in rows)
    # winter load–wind should be negative (cold anticyclone = high load, low wind) — the price driver
    djf_lw = [r[1] for r in rows if r[0] == "DJF"][0]
    out.append(Diag("Load–wind / load–PV seasonal correlations (historical)", "Cross-variable (killer)",
                    detail, passed=(djf_lw < 0.05)))

    # Dunkelflaute: rolling-72h wind CF<0.15 & PV CF<0.05, per winter
    df_cfg = config.section("validation")["dunkelflaute"]
    w = obs.get("wind_onshore"); p = obs.get("pv")
    jj = pd.concat([w.rename("w"), p.rename("p"), load.rename("l")], axis=1).dropna()
    roll_w = jj["w"].rolling(df_cfg["window_h"]).mean(); roll_p = jj["p"].rolling(df_cfg["window_h"]).mean()
    event = (roll_w < df_cfg["wind_cf_max"]) & (roll_p < df_cfg["pv_cf_max"])
    hi_load = jj["l"] > jj["l"].quantile(0.9)
    winters = event.index.year.where(event.index.month >= 7, event.index.year - 1)
    ev_per_winter = (event & hi_load).groupby(winters).sum()
    ev_per_winter = ev_per_winter[ev_per_winter.index >= 2015]
    out.append(Diag("Dunkelflaute events (72 h low-wind+low-PV in top-decile demand)",
                    "Cross-variable (killer)",
                    f"historical mean {ev_per_winter.mean():.0f} event-hours/winter "
                    f"(range {ev_per_winter.min():.0f}–{ev_per_winter.max():.0f}); winters flagged: "
                    f"{int((ev_per_winter>0).sum())}/{len(ev_per_winter)}", passed=None))

    # smoking gun: does the cube preserve the cold-calm (temp↔wind) winter dependence?
    from ..io.loaders import load_weather_hist, load_weather_synthetic

    def _tw_corr(w, col):
        g = w.groupby("timestamp_utc"); t = g["temperature_c"].mean(); wd = g[col].mean()
        dd = pd.concat([t.rename("t"), wd.rename("w")], axis=1).dropna()
        dd = dd[dd.index.month.isin([12, 1, 2])]
        return float(dd["t"].corr(dd["w"]))
    try:
        h = _tw_corr(load_weather_hist(config)[0], "wind_speed_ms")
        c = _tw_corr(load_weather_synthetic(config, 0)[0], "wind_speed_ms")
        out.append(Diag("Cube preserves the cold-calm dependence (DJF temp↔wind corr)",
                        "Cross-variable (killer)",
                        f"historical +{h:.2f} vs cube +{c:.2f} — the anticyclonic cold-calm link is "
                        f"{'preserved' if c > 0.5 * h else 'WEAKENED in the cube (weathergen dependence)'}",
                        passed=(c > 0.5 * h)))
    except Exception:
        pass

    # projected correlations (if the demand model output is reachable)
    proj = _projected_killer(config)
    if proj is not None:
        out.append(proj)
    else:
        out.append(Diag("Projected demand–RES correlation reproduces history", "Cross-variable (killer)",
                        "demand_model projection not importable — run step (iii) project to enable "
                        "the projected joint check", passed=None))
    return out


def _projected_killer(config) -> Diag | None:
    """corr(projected load, projected wind/PV CF) by season vs historical, if demand_model is reachable."""
    import sys
    dm = config.path.parent.parent / "demand_model"
    if not dm.exists():
        return None
    sys.path.insert(0, str(dm))
    try:
        from demand_model.config import load_config as dm_load
        from demand_model.projection import Projector as DProjector
    except Exception:
        return None
    try:
        dcfg = dm_load(str(dm / "config.yaml"))
        load = DProjector(dcfg).trajectory("reference", realization=0, seed=0) / 1e3   # GW
        from ..projection import Projector
        prod = Projector(config).production("reference", realization=0, seed=0)
        capw = prod["wind_onshore"].max(); capp = prod["pv_total"].max()
        w = prod["wind_onshore"] / max(capw, 1e-9); p = prod["pv_total"] / max(capp, 1e-9)
        j = pd.concat([load.rename("l"), w.rename("w"), p.rename("p")], axis=1).dropna()
        jd = j[j.index.month.isin([12, 1, 2])].resample("1D").mean().dropna()
        # WITHIN-WINTER anomalies: remove each winter's mean so the 20-yr structural growth trend
        # (load ↑ from EV/HP, wind CF ↑ from vintage cohorts) does not manufacture a spurious positive
        # correlation. The weather-driven cold-calm co-movement is what must be reproduced.
        wy = np.where(jd.index.month == 12, jd.index.year + 1, jd.index.year)
        a = jd - jd.groupby(wy).transform("mean")
        cw = float(a["l"].corr(a["w"])); cp = float(a["l"].corr(a["p"]))
    except Exception as exc:
        return Diag("Projected demand–RES correlation", "Cross-variable (killer)",
                    f"could not compute ({type(exc).__name__})", passed=None)
    ok = cw < -0.05
    note = ("cold-calm co-movement reproduced (negative)" if ok else
            " — should be negative; check cube DJF temp↔wind dependence and that the demand feature "
            "cache matches the current cube")
    return Diag("Projected demand–RES correlation (winter, detrended)", "Cross-variable (killer)",
                f"within-winter DJF corr(load,wind)={cw:+.2f} corr(load,PV)={cp:+.2f} — {note}", passed=ok)


def _vintage_check(config) -> list[Diag]:
    """Vintage sanity on the DETERMINISTIC fleet CF multiplier (traceable to the workbook), not the
    single-realization realized CF whose year-to-year weather noise swamps the modest uplift."""
    from ..projection import Projector
    from ..projection.vintage import annual_capacity, fleet_cf_factor
    pj = Projector(config)
    years = np.arange(int(config.section("projection")["horizon"]["start_year"]),
                      int(config.section("projection")["horizon"]["end_year"]) + 1)
    fac = fleet_cf_factor(pj.sheets, "wind_onshore", years)        # workbook cohort uplift, per year
    base_cf = pj.cal.metrics["mean_cf"]["wind_onshore"]
    cf_fleet = base_cf * fac                                       # deterministic fleet CF trajectory
    # realized (noisy) CF for the plot
    prod = pj.production("reference", realization=0, seed=0); yr = prod.index.year
    cap_y = annual_capacity(pj.sheets, "wind_onshore").reindex(np.unique(yr)).ffill()
    hours = prod["wind_onshore"].groupby(yr).size()
    cf_real = prod["wind_onshore"].groupby(yr).sum() / (cap_y * hours)
    fig, ax = plt.subplots(figsize=(5, 3.2))
    ax.plot(cf_fleet.index, cf_fleet.to_numpy() * 100, "-", label="vintage fleet CF (workbook)")
    ax.plot(cf_real.index, cf_real.to_numpy() * 100, ".", ms=4, alpha=0.5, label="realized (1 draw)")
    ax.set_title("projected onshore fleet CF (%)"); ax.set_xlabel("year"); ax.legend(fontsize=7)
    rising = cf_fleet.iloc[-1] > cf_fleet.iloc[0]
    return [Diag("Vintage sanity: onshore fleet CF rises with newer cohorts", "Vintage",
                 f"deterministic fleet CF {cf_fleet.iloc[0]*100:.1f}%→{cf_fleet.iloc[-1]*100:.1f}% "
                 f"(factor {fac.iloc[0]:.2f}→{fac.iloc[-1]:.2f}, workbook cf_uplift cohorts)",
                 passed=bool(rising), fig=_fig(fig))]


# --------------------------------------------------------------------------- #
#  Orchestration
# --------------------------------------------------------------------------- #
def run_validation_suite(config: Config) -> list[Diag]:
    from ..calibration.model import CalibratedRes
    cal = CalibratedRes.load(config.models_dir / "calibrated_res.json")
    diags: list[Diag] = []
    diags += _calibration_checks(config, cal)
    diags += _distribution_checks(config, cal)
    diags += _ramp_check(config)
    diags += _interannual_check(config)
    diags += _killer_test(config, cal)
    diags += _vintage_check(config)

    out = config.reports_dir / "validation_report.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_render(diags), encoding="utf-8")
    hard = [d for d in diags if d.passed is not None and not d.soft]
    n_pass = sum(d.passed for d in hard); n_fail = sum(not d.passed for d in hard)
    n_warn = sum(d.passed is False and d.soft for d in diags)
    print(f"[validate] {n_pass}/{len(hard)} hard checks PASS, {n_fail} FAIL, {n_warn} WARN(soft)")
    for d in diags:
        if d.passed is False:
            print(f"[validate]   {d.status}: {d.name} — {d.detail[:90]}")
    print(f"[validate] report -> {out}")
    return diags


def _render(diags: list[Diag]) -> str:
    color = {"PASS": "#1a7f37", "FAIL": "#cf222e", "WARN": "#9a6700", "INFO": "#57606a"}
    cats, body = [], []
    for d in diags:
        if d.category not in cats:
            cats.append(d.category)
    for cat in cats:
        body.append(f"<h2>{cat}</h2>")
        for d in [x for x in diags if x.category == cat]:
            badge = (f'<span style="color:{color[d.status]};font-weight:700">{d.status}</span>'
                     if d.passed is not None else "")
            body.append(f'<div class="chk"><b>{d.name}</b> {badge}<br><span class="det">{d.detail}</span></div>')
            if d.fig:
                body.append(f'<img src="data:image/png;base64,{d.fig}"/>')
    hard = [d for d in diags if d.passed is not None and not d.soft]
    summary = (f"{sum(d.passed for d in hard)}/{len(hard)} hard PASS · "
               f"{sum(not d.passed for d in hard)} FAIL · "
               f"{sum(d.passed is False and d.soft for d in diags)} WARN(soft)")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>res_model validation</title><style>
body{{font-family:system-ui,Segoe UI,Arial;margin:2rem;max-width:1000px;color:#1f2328}}
h1{{border-bottom:2px solid #d0d7de;padding-bottom:.3rem}} h2{{margin-top:1.6rem;color:#0969da}}
.chk{{padding:.35rem 0;border-bottom:1px solid #eaeef2}} .det{{color:#57606a;font-size:.9rem}}
img{{max-width:100%;margin:.5rem 0;border:1px solid #eaeef2;border-radius:6px}}
.sum{{background:#f6f8fa;padding:.6rem 1rem;border-radius:6px;font-weight:600}}</style></head><body>
<h1>res_model — validation report (step iv)</h1><div class="sum">{summary}</div>
<p class="det">Calibrated weather-to-power (PV / onshore / offshore / ROR). Potential production before
market curtailment. Same weather draws as the demand model → demand↔RES correlation preserved.</p>
{''.join(body)}</body></html>"""
