"""Phase 6 — projection engine.

Per scenario × weather realization:
  deterministic CF (calibrated chains on the cube weather) → + stochastic residual (seeded) →
  × vintage fleet factor × capacity(year) → potential production (MW), per technology & PV segment.

Coherent draws: realization k uses weather draw k (the SAME cube the demand model consumes) and seed k,
so demand↔RES correlations propagate. Output = potential production before market curtailment
(curtailment = step vi). PV segments (utility / distributed / BTM) are kept separate and an automated
double-counting check reconciles them against the scenario PV fleet. Partitioned Parquet + full metadata.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from powersim_core import lake

from ..config import Config
from .vintage import annual_capacity, fleet_cf_factor

# capacity segment -> which deterministic CF drives it
CF_MAP = {"pv_utility": "pv", "pv_distributed": "pv", "pv_btm": "pv",
          "wind_onshore": "wind_onshore",
          "wind_offshore_fixed": "wind_offshore", "wind_offshore_floating": "wind_offshore"}
VINTAGE_MAP = {"pv_btm": "pv_distributed"}          # BTM rooftop uses distributed cohorts
PV_SEGMENTS = ["pv_utility", "pv_distributed", "pv_btm"]


def _cap_hourly(sheets, tech, index, scenario) -> np.ndarray:
    cap = annual_capacity(sheets, tech, scenario)
    if cap.empty:
        return np.zeros(len(index))
    yr = index.year.to_numpy() + (index.dayofyear.to_numpy() - 1) / 365.25
    xp = cap.index.to_numpy().astype(float); fp = cap.to_numpy().astype(float)
    return np.interp(yr, xp, fp, left=fp[0], right=fp[-1])


def _factor_hourly(factor_by_year: pd.Series, index) -> np.ndarray:
    return np.array([factor_by_year.get(y, factor_by_year.iloc[-1]) for y in index.year])


def _hydro_capacity_mw(config: Config) -> float:
    import sqlite3
    d = config.section("data")
    con = sqlite3.connect(config.resolve(d["sqlite_path"]))
    try:
        v = pd.read_sql(f'SELECT value FROM "{d["production"]["capacity_type_table"]}" '
                        "WHERE series_key = ? AND value IS NOT NULL ORDER BY ts_utc DESC LIMIT 1",
                        con, params=[d["production"]["series_keys"]["hydro_ror"]])
    finally:
        con.close()
    return float(v["value"][0]) if len(v) else 11000.0


class Projector:
    """Loads the calibrated + residual models and the workbook once; serves coherent production draws."""

    def __init__(self, config: Config):
        from ..calibration.model import CalibratedRes
        from ..io.assumptions import load_assumptions
        from ..stochastic import ResidualModel
        self.config = config
        self.cal = CalibratedRes.load(config.models_dir / "calibrated_res.json")
        self.residual = ResidualModel.load(config.models_dir / "residual_res.json")
        self.sheets = load_assumptions(config.resolve(config.section("assumptions")["workbook"]))
        self.hydro_cap = _hydro_capacity_mw(config)
        self._cf: dict[int, pd.DataFrame] = {}

    def deterministic_cf(self, realization: int = 0) -> pd.DataFrame:
        if realization not in self._cf:
            from .drivers import projection_drivers
            d = projection_drivers(self.config, realization)
            cf = pd.DataFrame({
                "pv": self.cal.apply_pv(d["pv_raw"]),
                "wind_onshore": self.cal.apply_onshore(d["w100_nat"]),
                "wind_offshore": self.cal.apply_offshore(d["offshore_wind"]),
                "hydro_ror": self.cal.apply_hydro(d["precip_nat"], d["temp_nat"]),
            }, index=d.index).dropna()
            self._cf[realization] = self._anchor(cf)
        return self._cf[realization]

    def _anchor(self, cf: pd.DataFrame) -> pd.DataFrame:
        """Anchor each technology's projected CF *level* to its calibrated national mean. The synthetic
        cube's cloud/precip come from a different product (ERA5-derived) than the SYNOP fields the PV /
        hydro chains were calibrated on, which shifts the *level* (not the shape). A single per-tech
        scale corrects that while preserving the cube-driven temporal shape (and demand coherence).
        Wind, calibrated on ERA5, is already consistent so its scale ≈ 1."""
        target = self.cal.metrics["mean_cf"]
        out = cf.copy()
        self._anchor_scale = {}
        for t in cf.columns:
            tgt = target.get(t)
            m = float(cf[t].mean())
            s = float(tgt / m) if (tgt and m > 1e-6) else 1.0
            self._anchor_scale[t] = round(s, 3)
            out[t] = (cf[t] * s).clip(0.0, 1.0)
        return out

    def cf_draw(self, realization: int, seed: int | None, with_residual: bool) -> pd.DataFrame:
        from powersim_core.rng import draw_rng
        cf = self.deterministic_cf(realization)
        if not with_residual:
            return cf
        draw_id = realization if seed is None else seed
        sim = self.residual.simulate(cf, n_paths=1, rng=draw_rng(self.config.seed, draw_id))  # F4 authority
        return pd.DataFrame({t: sim[t]["path_000"] for t in sim}, index=cf.index)

    def production(self, scenario: str = "reference", realization: int = 0, seed: int | None = None,
                   with_residual: bool = True) -> pd.DataFrame:
        """Hourly potential production (MW) per technology segment + national total, one coherent draw."""
        cf = self.cf_draw(realization, seed, with_residual)
        idx = cf.index
        out = {}
        for seg, drv in CF_MAP.items():
            fac = fleet_cf_factor(self.sheets, VINTAGE_MAP.get(seg, seg),
                                  np.unique(idx.year), scenario)
            cap = _cap_hourly(self.sheets, seg, idx, scenario)
            out[seg] = np.clip(cf[drv].to_numpy(), 0, None) * _factor_hourly(fac, idx) * cap
        out["hydro_ror"] = cf["hydro_ror"].to_numpy() * self.hydro_cap
        prod = pd.DataFrame(out, index=idx)
        prod["pv_total"] = prod[PV_SEGMENTS].sum(axis=1)
        prod["wind_offshore"] = prod[["wind_offshore_fixed", "wind_offshore_floating"]].sum(axis=1)
        prod["national_total"] = (prod["pv_total"] + prod["wind_onshore"] + prod["wind_offshore"]
                                  + prod["hydro_ror"])
        return prod

    # -------------------------------------------------------------- double-count (§2.C)
    def double_count_report(self, scenario: str = "reference", realization: int = 0) -> dict:
        """Reconcile PV segments against the scenario PV fleet; expose BTM (netted in demand, step iii)."""
        prod = self.production(scenario, realization, with_residual=False)
        e = {s: float(prod[s].sum() / 1e6) for s in PV_SEGMENTS}          # TWh over the horizon
        e["pv_total"] = float(prod["pv_total"].sum() / 1e6)
        e["segments_sum"] = e["pv_utility"] + e["pv_distributed"] + e["pv_btm"]
        e["reconciled"] = bool(abs(e["pv_total"] - e["segments_sum"]) < 1e-6)
        e["btm_pv_generation_twh"] = e["pv_btm"]                          # for demand-netting reconciliation
        return e


def project_all(config: Config) -> dict[str, pd.DataFrame]:
    """Project every scenario × realization; save partitioned Parquet + summary + double-count report."""
    from ..io.loaders import load_weather_synthetic
    from ..meta import run_metadata
    pc = config.section("projection")
    scenarios = config.section("assumptions").get("scenarios", ["reference"])
    pj = Projector(config)
    outdir = config.output_dir; outdir.mkdir(parents=True, exist_ok=True)
    save_hourly = bool(pc.get("save_hourly", True))

    # realizations available in the cube
    try:
        _, _ = load_weather_synthetic(config, 0)
        n_real = 1
    except Exception:
        n_real = 1

    summaries = {}
    for sc in scenarios:
        dc = pj.double_count_report(sc, 0)
        for r in range(n_real):
            prod = pj.production(sc, realization=r, seed=r)
            meta = run_metadata(config, weather_draw=r, seed=r)
            if save_hourly:
                df = prod.copy()
                for k, v in meta.items():
                    df.attrs[k] = v
                lake.write_table(df, "res", "production", scenario=sc, realization=r)
            summ = _annual_summary(prod, config)
            summ.to_csv(outdir / f"res_summary_{sc}_r{r}.csv")
            summaries[f"{sc}_r{r}"] = summ
            y0, y1 = int(summ.index.min()), int(summ.index.max())
            print(f"[project:{sc} r{r}] {y0}-{y1} | "
                  f"PV {summ['pv_total_twh'].iloc[0]:.0f}->{summ['pv_total_twh'].iloc[-1]:.0f} | "
                  f"onshore {summ['wind_onshore_twh'].iloc[0]:.0f}->{summ['wind_onshore_twh'].iloc[-1]:.0f} | "
                  f"offshore {summ['wind_offshore_twh'].iloc[-1]:.0f} | ROR {summ['hydro_ror_twh'].iloc[-1]:.0f} TWh")
        print(f"[project:{sc}] PV double-count: segments_sum {dc['segments_sum']:.1f} = "
              f"pv_total {dc['pv_total']:.1f} TWh ({'OK' if dc['reconciled'] else 'MISMATCH'}); "
              f"BTM gen {dc['btm_pv_generation_twh']:.1f} TWh (netted in demand step iii)")
    print(f"[project] outputs -> {outdir}")
    return summaries


def _annual_summary(prod: pd.DataFrame, config: Config) -> pd.DataFrame:
    yr = prod.index.year
    cols = ["pv_total", "wind_onshore", "wind_offshore", "hydro_ror", "national_total"]
    rows = {f"{c}_twh": prod[c].groupby(yr).sum() / 1e6 for c in cols}
    rows["national_peak_gw"] = prod["national_total"].groupby(yr).max() / 1e3
    out = pd.DataFrame(rows); out.index.name = "year"
    return out.round(3)
