"""DM Phase 5 — projection engine.

Assembles the projected hourly demand for a scenario:

    load_net(t) =  Σ_g  D_g(year(t)) · component_g(t)          # rescaled statistical core
                 + EV(t) + electrolysis + datacentres + other  # bottom-up new loads
                 − BTM-PV self-consumption(t)                   # behind-the-meter netting
                 [+ ε(t)]                                       # one seeded residual draw (per trajectory)

The demand model is a **per-weather-scenario transducer**: one weather realization → one coherent
demand trajectory. The Monte-Carlo risk distribution lives in the *outer* weather→demand→price loop,
so a full trajectory is ``deterministic_net + one residual draw`` seeded off the draw index
(``project_trajectory`` / ``Projector.trajectory``). The residual p50/p95 in the annual summary is a
**within-weather diagnostic** of the ε layer only — NOT the risk band.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from powersim_core import lake

from ..config import Config
from .bottomup import btm_pv_netting, ev_load, flat_new_loads
from .drivers import Drivers


def _core_components(model, feat: pd.DataFrame) -> pd.DataFrame:
    """Evaluate the calibrated separable components with a SINGLE design build (memory/speed)."""
    X = model._design(feat)
    coef = model.coef
    out = {}
    for g, cols in model.groups.items():
        c = [k for k in cols if k in X.columns]
        out[g] = X[c].to_numpy() @ coef.reindex(c).to_numpy()
    df = pd.DataFrame(out, index=feat.index)
    df["base"] = df["base"] + model.intercept              # intercept rides with the base
    return df


def deterministic_net(config: Config, model, feat: pd.DataFrame,
                      sheets: dict[str, pd.DataFrame], scenario: str) -> tuple[pd.Series, pd.DataFrame]:
    """Rescaled core + bottom-up − BTM-PV (NO residual). Returns (net_mw, component_parts)."""
    pc = config.section("projection")
    years = np.arange(int(pc["horizon"]["start_year"]), int(pc["horizon"]["end_year"]) + 1)
    drivers = Drivers(sheets=sheets, scenario=scenario, anchor_year=int(pc["anchor_year"]),
                      base_shares=pc["base_shares"], years=years)
    factors = drivers.component_factors()                  # year × [base, heat, cool, light]

    comps = _core_components(model, feat)
    yr = feat.index.year
    core = pd.Series(0.0, index=feat.index)
    for g in ("base", "heat", "cool", "light"):
        core = core + factors[g].reindex(yr).to_numpy() * comps[g]
    core = core + comps["anomaly"]                         # ~0 in the future (flags off)

    ev = ev_load(drivers, feat.index, pc)
    newl = flat_new_loads(drivers, feat.index)
    pv = btm_pv_netting(drivers, feat["GHI_nat"], float(pc["btm_pv_performance_ratio"]))
    net = (core + ev + newl - pv).rename("load_mw").clip(lower=0.0)

    parts = pd.DataFrame({"base": factors["base"].reindex(yr).to_numpy() * comps["base"],
                          "heat": factors["heat"].reindex(yr).to_numpy() * comps["heat"],
                          "cool": factors["cool"].reindex(yr).to_numpy() * comps["cool"],
                          "light": factors["light"].reindex(yr).to_numpy() * comps["light"],
                          "ev": ev, "new_loads": newl, "btm_pv": -pv}, index=feat.index)
    return net, parts


def _annual_summary(config: Config, net: pd.Series, parts: pd.DataFrame,
                    residual_model, index: pd.DatetimeIndex) -> pd.DataFrame:
    """Per-year energy (TWh), deterministic peak + a within-weather residual-peak DIAGNOSTIC (GW).

    The p50/p95 here vary ONLY the residual draw at fixed weather — a QC band on the ε layer, not the
    weather-driven risk envelope (that comes from the outer Monte-Carlo over weather scenarios)."""
    pc = config.section("projection")
    n_paths = int(pc.get("residual_paths", 20))
    from powersim_core.rng import substream
    peaks = {p: (net + residual_model.simulate(index, n_paths=1,
                                               rng=substream(config.seed, p, "peak_residual"))["path_000"]
                 ).groupby(index.year).max() for p in range(n_paths)}     # F4 authority per band
    peak_df = pd.DataFrame(peaks)

    yr = index.year
    rows = pd.DataFrame({
        "energy_twh": net.groupby(yr).sum() / 1e6,
        "peak_gw_det": net.groupby(yr).max() / 1e3,
        "peak_gw_residdiag_p50": peak_df.median(axis=1) / 1e3,
        "peak_gw_residdiag_p95": peak_df.quantile(0.95, axis=1) / 1e3,
        "load_factor": (net.groupby(yr).mean() / net.groupby(yr).max()),
        "hours": net.groupby(yr).size(),
    })
    for c in parts.columns:                                # component annual energy (TWh)
        rows[f"e_{c}_twh"] = parts[c].groupby(yr).sum() / 1e6
    rows.index.name = "year"
    return rows.round(3)


def project_scenario(config: Config, model, residual_model, feat: pd.DataFrame,
                     sheets: dict[str, pd.DataFrame], scenario: str) -> dict:
    net, parts = deterministic_net(config, model, feat, sheets, scenario)
    summary = _annual_summary(config, net, parts, residual_model, feat.index)
    return {"scenario": scenario, "net": net, "parts": parts, "summary": summary}


# --------------------------------------------------------------------------- #
#  Coherent per-draw interface for the downstream price step
# --------------------------------------------------------------------------- #
class Projector:
    """Loads the calibrated model, residual model and scenario workbook once, then serves coherent
    per-draw demand trajectories. Deterministic nets are cached per (scenario, realization) so only
    the (cheap) residual draw varies across replications of the same weather scenario.

    Typical use by the price orchestrator::

        pj = Projector(config)
        demand_k = pj.trajectory(scenario="reference", realization=k, seed=k)   # weather k ⊕ ε k
    """

    def __init__(self, config: Config):
        from ..calibration.model import CalibratedModel
        from ..io.assumptions import load_assumptions
        from ..residual import ResidualModel
        self.config = config
        self.model = CalibratedModel.load(config.models_dir / "calibrated.json")
        self.residual_model = ResidualModel.load(config.models_dir / "residual.json")
        self.sheets = load_assumptions(config.resolve(config.section("assumptions")["workbook"]))
        self._feat: dict[int, pd.DataFrame] = {}
        self._net: dict[tuple[str, int], pd.Series] = {}

    def features(self, realization: int = 0) -> pd.DataFrame:
        from .weather import projection_features
        if realization not in self._feat:
            self._feat[realization] = projection_features(self.config, realization=realization)
        return self._feat[realization]

    def deterministic_net(self, scenario: str, realization: int = 0) -> pd.Series:
        key = (scenario, realization)
        if key not in self._net:
            net, _ = deterministic_net(self.config, self.model, self.features(realization),
                                       self.sheets, scenario)
            self._net[key] = net
        return self._net[key]

    def trajectory(self, scenario: str, realization: int = 0, seed: int | None = None,
                   with_residual: bool = True) -> pd.Series:
        """One coherent hourly demand path (MW): deterministic net + one seeded residual draw.

        ``seed`` defaults to ``realization`` so draw k deterministically reproduces weather k ⊕ ε k;
        set ``with_residual=False`` to get the deterministic core alone."""
        net = self.deterministic_net(scenario, realization)
        if not with_residual:
            return net.rename("load_mw")
        from powersim_core.rng import draw_rng
        s = realization if seed is None else seed
        eps = self.residual_model.simulate(net.index, n_paths=1, rng=draw_rng(self.config.seed, s))["path_000"]
        return (net + eps).clip(lower=0.0).rename("load_mw")


def project_trajectory(config: Config, scenario: str = "reference", realization: int = 0,
                       seed: int | None = None, with_residual: bool = True) -> pd.Series:
    """Convenience one-shot: a single coherent demand trajectory (MW). For repeated draws build a
    :class:`Projector` once and call ``.trajectory`` (it caches the deterministic net)."""
    return Projector(config).trajectory(scenario, realization=realization, seed=seed,
                                        with_residual=with_residual)


def run_projection(config: Config) -> dict[str, pd.DataFrame]:
    """Project every configured scenario; save summaries + (optionally) deterministic hourly load."""
    pc = config.section("projection")
    pj = Projector(config)
    scenarios = config.section("assumptions").get("scenarios", ["reference"])
    save_hourly = set(pc.get("outputs", {}).get("save_hourly_scenarios", []))
    outdir = config.output_dir; outdir.mkdir(parents=True, exist_ok=True)

    summaries: dict[str, pd.DataFrame] = {}
    for sc in scenarios:
        res = project_scenario(config, pj.model, pj.residual_model, pj.features(0), pj.sheets, sc)
        summaries[sc] = res["summary"]
        res["summary"].to_csv(outdir / f"projection_summary_{sc}.csv")
        if sc in save_hourly:                              # deterministic core (residual added per draw)
            lake.write_table(res["net"].to_frame("load_mw"), "demand", "projection_hourly", scenario=sc)
        s = res["summary"]
        print(f"[project:{sc}] {int(s.index.min())}-{int(s.index.max())} | "
              f"energy {s['energy_twh'].iloc[0]:.1f}->{s['energy_twh'].iloc[-1]:.1f} TWh | "
              f"peak(det) {s['peak_gw_det'].iloc[0]:.1f}->{s['peak_gw_det'].iloc[-1]:.1f} GW | "
              f"resid-diag p95 {s['peak_gw_residdiag_p95'].iloc[-1]:.1f} GW")
    print(f"[project] summaries -> {outdir}")
    return summaries
