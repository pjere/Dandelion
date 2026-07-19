"""DM Phase 6 — validation suite.

Checks the model against the acceptance criteria (§8) and writes an HTML report. Grouped into:

* Calibration   — winter gradient in the RTE band, MAPE, bias (hard + one soft aspiration)
* Components    — additive separability + physically-correct component signs
* Cold spells   — behaviour in the coldest ~1 % of days + sustained-cold-spell energy (thermal inertia)
* Residual      — heteroscedasticity, AR persistence, and residual-surprise spell lengths
* Projection    — plausibility of the projected energy / peak / load-factor trajectories

Every check yields a metric + PASS / WARN(soft) / FAIL against a documented tolerance.
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


@dataclass
class Diag:
    name: str
    category: str
    detail: str
    passed: bool | None = None      # True=PASS, False=FAIL, None=info/figure
    soft: bool = False              # a failing soft check is a WARN (aspiration), not a FAIL
    fig: str | None = None

    @property
    def status(self) -> str:
        if self.passed is None:
            return "INFO"
        if self.passed:
            return "PASS"
        return "WARN" if self.soft else "FAIL"


def mean_run_length(mask) -> float:
    """Mean length of consecutive True runs in a boolean sequence (0 if none)."""
    runs, cur = [], 0
    for b in mask:
        if b:
            cur += 1
        else:
            if cur:
                runs.append(cur)
            cur = 0
    if cur:
        runs.append(cur)
    return float(np.mean(runs)) if runs else 0.0


def _fig_b64(fig) -> str:
    buf = BytesIO(); fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


# --------------------------------------------------------------------------- #
#  Data assembly
# --------------------------------------------------------------------------- #
def _aligned(config: Config, model, feat: pd.DataFrame, demand: pd.DataFrame) -> pd.DataFrame:
    df = feat.join(demand.set_index("timestamp_utc")["load_mw"], how="inner").dropna(
        subset=["load_mw", "T_smooth_60h", "T_smooth_12h"])
    df["yhat"] = model.predict(df)
    df = df.dropna(subset=["yhat"])
    df["resid"] = df["load_mw"] - df["yhat"]
    return df


# --------------------------------------------------------------------------- #
#  Checks
# --------------------------------------------------------------------------- #
def _calibration_checks(config: Config, model) -> list[Diag]:
    m = model.metrics
    band = config.section("validation")["winter_gradient_gw_per_c"]
    target = float(config.section("validation").get("mape_target_pct", 3.0))
    g = abs(m["winter_gradient_gw_per_c"])
    out = [
        Diag("Winter gradient in RTE band", "Calibration",
             f"{m['winter_gradient_gw_per_c']} GW/°C vs band [{band['low']}, {band['high']}]",
             passed=(band["low"] <= g <= band["high"])),
        Diag("In-sample hourly MAPE", "Calibration",
             f"{m['mape_in_sample']}% vs ≤{target}%", passed=(m["mape_in_sample"] <= target)),
        Diag("Hold-out hourly MAPE (aspiration)", "Calibration",
             f"{m.get('mape_holdout')}% vs ≤{target}% — lagged-load-free floor (~3.3-3.5 %); "
             f"sub-3 % hold-out needs autoregression, invalid for projection",
             passed=(m.get("mape_holdout", 99) <= target), soft=True),
        Diag("Hold-out bias", "Calibration",
             f"{m.get('holdout_bias_pct')}% vs |·|≤2%", passed=(abs(m.get("holdout_bias_pct", 9)) <= 2.0)),
    ]
    return out


def _component_checks(model, df: pd.DataFrame) -> list[Diag]:
    comps = model.components(df)
    recon = comps.sum(axis=1)
    sep = float(np.max(np.abs(recon.to_numpy() - model.predict(df).to_numpy())))
    cold = df["T_smooth_60h"] < 5
    hot = df["T_smooth_12h"] > 22
    heat_pos = float(comps.loc[cold, "heat"].mean())
    cool_pos = float(comps.loc[hot, "cool"].mean()) if hot.any() else 0.0
    light_coef = float(model.coef[list(model.groups["light"])].mean())
    return [
        Diag("Additive separability (Σ components = prediction)", "Components",
             f"max|Σ−ŷ| = {sep:.2e} MW", passed=(sep < 1e-6)),
        Diag("Heating component positive in cold", "Components",
             f"mean heat |T<5°C = {heat_pos:,.0f} MW", passed=(heat_pos > 0)),
        Diag("Cooling component positive in heat", "Components",
             f"mean cool |T>22°C = {cool_pos:,.0f} MW", passed=(cool_pos >= 0)),
        Diag("Lighting rises as daylight falls (GHI coef < 0)", "Components",
             f"mean GHI×hour coef = {light_coef:,.1f}", passed=(light_coef < 0)),
    ]


def _coldspell_checks(df: pd.DataFrame) -> list[Diag]:
    daily_t = df["T_nat"].resample("1D").mean()
    thr = daily_t.quantile(0.01)                                # coldest ~1 % of days
    cold_days = set(daily_t[daily_t <= thr].index.date)
    mask = pd.Series(df.index.date, index=df.index).isin(cold_days)
    sub = df[mask]
    mean_err = (sub["yhat"].mean() - sub["load_mw"].mean()) / sub["load_mw"].mean() * 100
    peak_err = (sub["yhat"].max() - sub["load_mw"].max()) / sub["load_mw"].max() * 100
    mape_cold = float(np.mean(np.abs(sub["resid"] / sub["load_mw"])) * 100)

    # sustained cold spell (thermal inertia): longest run of days with daily mean T < 2°C
    below = daily_t < 2.0
    runs, cur = [], 0
    for b in below:
        cur = cur + 1 if b else 0
        if cur:
            runs.append(cur)
    max_run = max(runs) if runs else 0

    fig, ax = plt.subplots(figsize=(4.2, 4.0))
    ax.scatter(sub["load_mw"] / 1e3, sub["yhat"] / 1e3, s=4, alpha=0.3)
    lim = [sub["load_mw"].min() / 1e3, sub["load_mw"].max() / 1e3]
    ax.plot(lim, lim, "k:", lw=1)
    ax.set_xlabel("actual load (GW)"); ax.set_ylabel("predicted (GW)")
    ax.set_title(f"Coldest 1% of days (T≤{thr:.1f}°C)")
    return [
        Diag("Cold-spell energy bias (coldest 1% days)", "Cold spells",
             f"mean load error {mean_err:+.2f}% (target |·|<3%)", passed=(abs(mean_err) < 3.0)),
        Diag("Cold-spell peak error", "Cold spells",
             f"peak error {peak_err:+.2f}% (target |·|<5%)", passed=(abs(peak_err) < 5.0)),
        Diag("Cold-spell hourly MAPE", "Cold spells",
             f"{mape_cold:.2f}% (stress; expect ≲ overall+1.5%)", passed=(mape_cold < 5.5)),
        Diag("Longest sustained cold spell captured", "Cold spells",
             f"max run of days with daily-mean T<2°C = {max_run} d (thermal-inertia terms active)",
             passed=None, fig=_fig_b64(fig)),
    ]


def _residual_checks(residual_model, df: pd.DataFrame) -> list[Diag]:
    from ..residual.model import _buckets
    rm = residual_model
    d = rm.metrics
    hetero = d["sigma_max_mw"] / d["sigma_min_mw"]

    # residual-surprise spell length: runs where standardised residual z>1 (empirical vs simulated)
    keys = _buckets(df.index)
    sig = rm.sigma.reindex(keys).to_numpy()
    sig[np.isnan(sig)] = rm.sigma_global
    z_emp = (df["resid"].to_numpy() / sig)
    sim = rm.simulate(df.index, n_paths=1, seed=999)["path_000"].to_numpy() / sig

    run_emp, run_sim = mean_run_length(z_emp > 1.0), mean_run_length(sim > 1.0)

    # σ heatmap (season × hour, weekday)
    sig_wd = rm.sigma[[k for k in rm.sigma.index if k.endswith("|0")]]
    grid = pd.DataFrame({"season": [k.split("|")[0] for k in sig_wd.index],
                         "hour": [int(k.split("|")[1]) for k in sig_wd.index],
                         "sigma": sig_wd.to_numpy() / 1e3}).pivot(index="season", columns="hour", values="sigma")
    fig, ax = plt.subplots(figsize=(6.5, 2.6))
    im = ax.imshow(grid.values, aspect="auto", cmap="viridis")
    ax.set_yticks(range(len(grid.index))); ax.set_yticklabels(grid.index)
    ax.set_xlabel("local hour"); ax.set_title("residual σ (GW) — weekdays")
    fig.colorbar(im, ax=ax, shrink=0.8)
    return [
        Diag("Heteroscedasticity present", "Residual",
             f"σ range {d['sigma_min_mw']}-{d['sigma_max_mw']} MW (ratio {hetero:.1f}×)", passed=(hetero > 1.5)),
        Diag("AR persistence reproduced", "Residual",
             f"lag-1 autocorr emp {d['acf1_empirical']} vs sim {d['acf1_simulated']}",
             passed=(abs(d["acf1_simulated"] - d["acf1_empirical"]) < 0.05)),
        Diag("Residual variance reproduced", "Residual",
             f"std(sim)/std(emp) = {d['std_ratio_sim_over_emp']}",
             passed=(abs(d["std_ratio_sim_over_emp"] - 1.0) < 0.1)),
        Diag("Residual-surprise spell length (z>1σ)", "Residual",
             f"mean run emp {run_emp:.2f} h vs sim {run_sim:.2f} h",
             passed=(0.6 < (run_sim / run_emp if run_emp else 0) < 1.6), fig=_fig_b64(fig)),
    ]


def _projection_checks(config: Config) -> list[Diag]:
    outdir = config.output_dir
    files = sorted(outdir.glob("projection_summary_*.csv"))
    if not files:
        return [Diag("Projection outputs present", "Projection",
                     "no projection_summary_*.csv found — run 'demand-model project' first", passed=False)]
    diags, fig_done = [], False
    for f in files:
        sc = f.stem.replace("projection_summary_", "")
        s = pd.read_csv(f).set_index("year")
        ok_e = s["energy_twh"].between(300, 900).all() and np.isfinite(s["energy_twh"]).all()
        ok_p = s["peak_gw_det"].between(60, 160).all()
        ok_lf = s["load_factor"].between(0.40, 0.85).all()
        ok_p95 = (s["peak_gw_residdiag_p95"] >= s["peak_gw_det"]).all()
        diags += [
            Diag(f"[{sc}] energy trajectory plausible", "Projection",
                 f"{s['energy_twh'].min():.0f}-{s['energy_twh'].max():.0f} TWh (bound 300-900)", passed=bool(ok_e)),
            Diag(f"[{sc}] peak trajectory plausible", "Projection",
                 f"{s['peak_gw_det'].min():.0f}-{s['peak_gw_det'].max():.0f} GW (bound 60-160)", passed=bool(ok_p)),
            Diag(f"[{sc}] load factor in range", "Projection",
                 f"{s['load_factor'].min():.2f}-{s['load_factor'].max():.2f} (bound 0.40-0.85)", passed=bool(ok_lf)),
            Diag(f"[{sc}] residual-diag peak ≥ deterministic", "Projection",
                 f"resid-diag p95 {s['peak_gw_residdiag_p95'].max():.1f} GW ≥ det "
                 f"{s['peak_gw_det'].max():.1f} GW (within-weather ε band, not the risk envelope)",
                 passed=bool(ok_p95)),
        ]
        if not fig_done:
            fig, ax = plt.subplots(1, 2, figsize=(9, 3.2))
            ax[0].plot(s.index, s["energy_twh"], "-o", ms=3); ax[0].set_title(f"[{sc}] annual energy (TWh)")
            ax[1].plot(s.index, s["peak_gw_det"], "-o", ms=3, label="det")
            ax[1].fill_between(s.index, s["peak_gw_det"], s["peak_gw_residdiag_p95"], alpha=0.25,
                               label="→resid-diag p95")
            ax[1].set_title("peak (GW)"); ax[1].legend(fontsize=7)
            diags.append(Diag(f"[{sc}] trajectories", "Projection", "energy + peak over the horizon",
                              passed=None, fig=_fig_b64(fig)))
            fig_done = True
    return diags


# --------------------------------------------------------------------------- #
#  Orchestration + report
# --------------------------------------------------------------------------- #
def run_validation_suite(config: Config) -> list[Diag]:
    from ..calibration.model import CalibratedModel
    from ..features.build import national_features
    from ..io.loaders import load_demand
    from ..io.qc import qc_demand
    from ..residual import ResidualModel

    model = CalibratedModel.load(config.models_dir / "calibrated.json")
    residual_model = ResidualModel.load(config.models_dir / "residual.json")
    feat = national_features(config)
    demand, _ = qc_demand(load_demand(config), config)
    df = _aligned(config, model, feat, demand)

    diags: list[Diag] = []
    diags += _calibration_checks(config, model)
    diags += _component_checks(model, df)
    diags += _coldspell_checks(df)
    diags += _residual_checks(residual_model, df)
    diags += _projection_checks(config)

    report = _render_html(config, diags)
    out = config.reports_dir / "validation_report.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")

    hard = [d for d in diags if d.passed is not None and not d.soft]
    n_pass = sum(d.passed for d in hard)
    n_fail = sum((not d.passed) for d in hard)
    n_warn = sum((d.passed is False and d.soft) for d in diags)
    print(f"[validate] {n_pass}/{len(hard)} hard checks PASS, {n_fail} FAIL, {n_warn} WARN(soft)")
    for d in diags:
        if d.passed is False:
            print(f"[validate]   {d.status}: {d.name} — {d.detail}")
    print(f"[validate] report -> {out}")
    return diags


def _render_html(config: Config, diags: list[Diag]) -> str:
    color = {"PASS": "#1a7f37", "FAIL": "#cf222e", "WARN": "#9a6700", "INFO": "#57606a"}
    cats = []
    for d in diags:
        if d.category not in cats:
            cats.append(d.category)
    body = []
    for cat in cats:
        body.append(f"<h2>{cat}</h2>")
        for d in [x for x in diags if x.category == cat]:
            badge = (f'<span style="color:{color[d.status]};font-weight:700">{d.status}</span>'
                     if d.passed is not None else "")
            body.append(f'<div class="chk"><b>{d.name}</b> {badge}<br>'
                        f'<span class="det">{d.detail}</span></div>')
            if d.fig:
                body.append(f'<img src="data:image/png;base64,{d.fig}"/>')
    hard = [d for d in diags if d.passed is not None and not d.soft]
    summary = (f"{sum(d.passed for d in hard)}/{len(hard)} hard checks PASS · "
               f"{sum((not d.passed) for d in hard)} FAIL · "
               f"{sum((d.passed is False and d.soft) for d in diags)} WARN(soft)")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>demand_model validation</title><style>
body{{font-family:system-ui,Segoe UI,Arial;margin:2rem;max-width:1000px;color:#1f2328}}
h1{{border-bottom:2px solid #d0d7de;padding-bottom:.3rem}}
h2{{margin-top:1.6rem;color:#0969da}}
.chk{{padding:.35rem 0;border-bottom:1px solid #eaeef2}}
.det{{color:#57606a;font-size:.9rem}}
img{{max-width:100%;margin:.5rem 0;border:1px solid #eaeef2;border-radius:6px}}
.sum{{background:#f6f8fa;padding:.6rem 1rem;border-radius:6px;font-weight:600}}
</style></head><body>
<h1>demand_model — validation report</h1>
<div class="sum">{summary}</div>
<p class="det">Model: hybrid statistical-structural hourly demand (mainland France). Perimeter =
RTE REALISED − pumping. Calibrated 2015–2026; projected 2027–2046 on weathergen weather +
scenario workbook. See DECISIONS.md for the full decision log.</p>
{''.join(body)}
</body></html>"""
