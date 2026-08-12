"""Weather-coherent projection net-load shapes (#77) — the provider behind the projection's `weather_shapes`
hook, so a projected year sees a *re-drawn* weathergen weather shape instead of the fixed 2019 one.

FR is exact: its demand (`demand_model`, step iii) and RES (`res_model`, step iv) models both consume the
SAME weathergen realization, so `fr_shape` just runs them and assembles the net load. Neighbours have no such
models, so `NeighbourWeatherModel` is a **reduced-form** stand-in fitted from history: neighbour load responds
to the FR national temperature (HDD/CDD + calendar), and neighbour must-take RES follows the zone's OWN
observed shape (#157) modulated by the FR draw's renewable anomaly vs FR normal — justified by the strong
spatial correlation of European weather (a cold, calm French day is usually cold and calm across the
interconnected core). It is weather-*coherent* (driven by the same FR draw) but not station-resolved; a full
build would extend weathergen to neighbour stations and fit per-zone demand/RES models (steps iii/iv ×6).
RES *levels* are set by the projected neighbour capacity (TYNDP, #76).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]     # repo root (…/PriceModeling)
_MUSTTAKE = ["solar", "wind_onshore", "wind_offshore", "hydro_ror"]

#: per-process caches (never pickled across the MC pool boundary — each worker fills its own). `fr_draw`
#: re-runs the FULL demand+RES horizon on every call, so a trajectory's 20 years MUST share one draw or the
#: projection is ~20× too slow; the neighbour fit is a one-shot OLS over history. Keyed by realization /
#: fit-year so distinct MC draws stay independent.
_DRAW_CACHE: dict[int, pd.DataFrame] = {}
_NB_MODEL_CACHE: dict = {}
#: neighbour structural growth fallback when a zone has no TYNDP row (mirrors projection._GROWTH).
_G_FALLBACK = {"demand": 0.008, "res": 0.045}


# ----------------------------------------------------------------- FR (exact) ---------------------------
def fr_draw(realization: int = 0) -> pd.DataFrame:
    """Run the FR demand + RES models on one weathergen realization → hourly [load_mw, res_mw, temp_nat] over
    the whole projection horizon (one coherent draw). `temp_nat` (projected FR national temperature) drives
    the reduced-form neighbour models; `res_mw` normalised gives the neighbour RES shape.

    Cached per realization (per process): the demand + RES models run ONCE per draw, so all of a trajectory's
    target years slice the same coherent draw instead of recomputing the whole horizon each year."""
    if realization in _DRAW_CACHE:
        return _DRAW_CACHE[realization]
    from demand_model.config import load_config as dm_load
    from demand_model.projection.engine import project_trajectory
    from res_model.config import load_config as rm_load
    from res_model.projection.drivers import projection_drivers
    from res_model.projection.engine import Projector

    rm_cfg = rm_load(str(_ROOT / "res_model" / "config.yaml"))
    load = project_trajectory(dm_load(str(_ROOT / "demand_model" / "config.yaml")),
                              "reference", realization=realization, with_residual=False)
    prod = Projector(rm_cfg).production("reference", realization=realization, with_residual=False)
    temp = projection_drivers(rm_cfg, realization)["temp_nat"]
    df = pd.DataFrame({"load_mw": load, "res_mw": prod["national_total"], "temp_nat": temp}).dropna()
    _DRAW_CACHE[realization] = df
    return df


def fr_shape(target_year: int, realization: int = 0, draw: pd.DataFrame | None = None) -> pd.DataFrame:
    """FR net-load shape [timestamp_utc, demand_mw, musttake_res_mw] for `target_year` from the coherent
    demand+RES draw."""
    d = draw if draw is not None else fr_draw(realization)
    y = d[d.index.year == target_year]
    return pd.DataFrame({"timestamp_utc": y.index, "demand_mw": y["load_mw"].to_numpy(),
                         "musttake_res_mw": y["res_mw"].to_numpy()}).reset_index(drop=True)


# -------------------------------------------------- neighbours (reduced-form) ----------------------------
def _fr_hist_weather(config) -> pd.DataFrame:
    """Historical FR national temperature (°C) hourly from master_hourly — the neighbour demand driver."""
    import sqlite3
    con = sqlite3.connect(config.resolve(config.section("data")["sqlite_path"]))
    try:
        df = pd.read_sql("SELECT ts_utc, meteo_temperature_c_fr AS temperature_c FROM master_hourly "
                         "WHERE meteo_temperature_c_fr IS NOT NULL", con)
    finally:
        con.close()
    df["timestamp_utc"] = pd.to_datetime(df["ts_utc"], utc=True)   # DB raw ts_utc → canonical (ADR-3)
    return df.set_index("timestamp_utc")[["temperature_c"]].sort_index()


def _design(temp: np.ndarray, idx: pd.DatetimeIndex) -> np.ndarray:
    """Demand design: [1, HDD, CDD, hour harmonics, weekend] — a temperature-response load model."""
    hdd = np.maximum(16.0 - temp, 0.0)                       # heating degrees below 16 °C
    cdd = np.maximum(temp - 22.0, 0.0)                       # cooling degrees above 22 °C
    h = idx.hour.to_numpy(float)
    wknd = (idx.dayofweek.to_numpy() >= 5).astype(float)
    return np.column_stack([np.ones(len(idx)), hdd, cdd,
                            np.sin(2 * np.pi * h / 24), np.cos(2 * np.pi * h / 24),
                            np.sin(4 * np.pi * h / 24), np.cos(4 * np.pi * h / 24), wknd])


def _norm_shape(series: pd.Series, year: int) -> np.ndarray:
    """A must-take RES hourly series → **unit-mean** numpy array over the full `year` calendar (missing hours
    → 0). The positional array is what a target year slices — the zone's own diurnal/seasonal RES climatology."""
    idx = pd.date_range(f"{year}-01-01", f"{year + 1}-01-01", freq="h", tz="UTC")[:-1]
    s = series.reindex(idx).fillna(0.0).to_numpy(float)
    m = s.mean()
    return s / m if m > 0 else np.ones_like(s)


class NeighbourWeatherModel:
    """Reduced-form per-zone demand + **own-weather** RES response to the FR national weather draw.

    Demand is the zone's own load regressed on the FR national temperature (spatially-correlated European
    weather; no neighbour station data exists). RES uses the zone's OWN observed must-take shape (#157) —
    German solar peaks at German solar hours, not French — modulated by the FR draw's renewable *anomaly vs
    FR normal*, which keeps it weather-coherent across zones and draws (a low-wind European draw lowers every
    zone together) while the diurnal/seasonal shape stays each zone's own. Levels come from projected
    capacity (TYNDP growth). A full build would station-resolve neighbours (steps iii/iv ×6)."""

    def __init__(self, load_coef: dict, res_beta: dict, res_shape: dict, fr_res_clim: np.ndarray):
        self.load_coef = load_coef        # zone -> OLS coef on _design (mean load level absorbed by intercept)
        self.res_beta = res_beta          # zone -> {mean_res_mw, mean_load_mw}: RES level anchor
        self.res_shape = res_shape        # zone -> unit-mean OWN must-take RES climatology (fit-year hourly)
        self.fr_res_clim = fr_res_clim    # unit-mean FR must-take RES climatology — the coherence baseline

    @staticmethod
    def fit(config, year: int = 2019) -> NeighbourWeatherModel:
        """Fit each neighbour's load↔FR-temp OLS and store its own must-take RES climatology (fit-year hourly),
        plus the FR RES climatology that the draw is compared against for coherence."""
        from .io.entsoe_hist import load_demand_hist, load_generation_hist
        from .neighbours.blocks import constituents

        temperature = _fr_hist_weather(config)
        load_coef, res_beta, res_shape = {}, {}, {}
        # FR own must-take RES climatology — the baseline the FR draw's anomaly is measured against (so a
        # windier-than-FR-normal draw lifts every neighbour's own shape proportionally). Falls back to a flat
        # unit shape if FR generation history is unavailable (then coherence ≈ the draw's own unit-mean shape).
        try:
            _frg = load_generation_hist(config, year, zones=constituents("FR"))
            _frmt = _frg[_frg["tech"].isin(_MUSTTAKE)].groupby("timestamp_utc")["gen_mw"].sum()
            fr_res_clim = _norm_shape(_frmt, year)
        except (KeyError, ValueError):
            fr_res_clim = np.ones(len(pd.date_range(f"{year}-01-01", f"{year + 1}-01-01", freq="h")) - 1)
        # GB used to be excluded alongside FR for want of ENTSO-E history; `pricemodeling.elexon` now
        # supplies its load and generation, so it fits like any other neighbour. FR stays out because it
        # is the reference whose weather drives every other zone's fit, not a zone being fitted.
        zones = [z for z in config.all_zones if z != "FR"]
        for z in zones:
            ld = load_demand_hist(config, year, zones=constituents(z)).groupby("timestamp_utc")["load_mw"].sum()
            j = pd.DataFrame({"load": ld}).join(temperature, how="inner").dropna()
            if len(j) < 500:
                continue
            X = _design(j["temperature_c"].to_numpy(), pd.DatetimeIndex(j.index))
            beta, *_ = np.linalg.lstsq(X, j["load"].to_numpy(), rcond=None)
            load_coef[z] = beta.tolist()
            # RES: the zone's OWN observed must-take shape (unit-mean) + its mean level (#157).
            g = load_generation_hist(config, year, zones=constituents(z))
            mt = g[g["tech"].isin(_MUSTTAKE)].groupby("timestamp_utc")["gen_mw"].sum()
            res_beta[z] = {"mean_res_mw": float(mt.mean()) if len(mt) else 0.0,
                           "mean_load_mw": float(ld.mean())}
            res_shape[z] = _norm_shape(mt, year)
        return NeighbourWeatherModel(load_coef, res_beta, res_shape, fr_res_clim)

    def shape(self, zone: str, target_year: int, fr_temp: pd.Series, fr_res_cf: pd.Series,
              load_growth: float = 1.0, res_growth: float = 1.0) -> pd.DataFrame | None:
        """Neighbour net-load [timestamp_utc, load_mw, musttake_res_mw] for `target_year`: load from the
        temperature response × growth; RES from the zone's OWN shape × the FR draw's renewable anomaly
        (draw/FR-climatology) × scaled mean × growth (#157 — own-weather, still draw-coherent)."""
        if zone not in self.load_coef:
            return None
        idx = fr_temp.index[fr_temp.index.year == target_year]
        if len(idx) == 0:
            return None
        n = len(idx)
        t = fr_temp.reindex(idx).to_numpy()
        load = (_design(t, pd.DatetimeIndex(idx)) @ np.array(self.load_coef[zone])) * load_growth
        # own-weather RES: zone's own climatology × draw-vs-FR-normal anomaly (bounded), positional-aligned.
        own = np.resize(self.res_shape[zone], n) if zone in self.res_shape else np.ones(n)
        draw = fr_res_cf.reindex(idx).to_numpy(float)
        draw = draw / (np.nanmean(draw) or 1.0)                    # unit-mean FR draw shape
        clim = np.resize(self.fr_res_clim, n)
        coher = np.clip(np.divide(draw, clim, out=np.ones_like(draw), where=clim > 1e-6), 0.0, 3.0)
        res = own * coher * self.res_beta[zone]["mean_res_mw"] * res_growth
        return pd.DataFrame({"timestamp_utc": idx, "load_mw": np.clip(load, 0, None),
                             "musttake_res_mw": np.clip(res, 0, None)}).dropna().reset_index(drop=True)


# -------------------------------------------------- assemble the hook payload ----------------------------
def all_weather_shapes(config, target_year: int, realization: int = 0, nb_model: NeighbourWeatherModel | None = None,
                       growth: dict | None = None) -> dict:
    """{zone: net-load df} for the projection's `weather_shapes` hook, from one coherent FR weathergen draw:
    FR exact (demand+RES models), neighbours reduced-form (fit `nb_model` once and pass it). `growth` =
    {zone: (load_growth, res_growth)} applies the structural level (e.g. TYNDP factors, #76) on top of the
    weather-driven shape; default 1.0. Zones the neighbour model couldn't fit are omitted → the projection
    falls back to their reference-year shape for those."""
    draw = fr_draw(realization)
    out = {"FR": fr_shape(target_year, draw=draw)}
    if nb_model is not None:
        temp = draw["temp_nat"]
        res_cf = draw["res_mw"]                              # FR national RES production → the neighbour shape
        for z in nb_model.load_coef:
            lg, rg = (growth or {}).get(z, (1.0, 1.0))
            s = nb_model.shape(z, target_year, temp, res_cf, load_growth=lg, res_growth=rg)
            if s is not None and not s.empty:
                out[z] = s
    return out


def _neighbour_model(config, fit_year: int = 2019) -> NeighbourWeatherModel:
    """Fit (and cache per process) the reduced-form neighbour model once — the OLS over `fit_year` history."""
    if fit_year not in _NB_MODEL_CACHE:
        _NB_MODEL_CACHE[fit_year] = NeighbourWeatherModel.fit(config, year=fit_year)
    return _NB_MODEL_CACHE[fit_year]


def default_weather_provider(config, target_year: int, draw: int = 0, ref_year: int = 2019,
                             fit_year: int = 2019) -> dict:
    """Top-level, picklable ``weather_provider(config, year, draw) -> {zone: net-load df}`` — the DEFAULT
    projection weather source (replaces the reshape-2019 path).

    FR is exact (demand+RES models on weathergen realization ``draw``); neighbours are the reduced-form
    stand-in, levelled to the target year by their TYNDP demand/RES factors (so the plan capacities set the
    level and the FR draw sets the shape). Everything is cached per process, so the FR draw and the neighbour
    fit are each paid once regardless of how many years/draws are requested. Picklable → usable as the
    ``run_ensemble`` weather_provider across the process pool."""
    from .tyndp import load_tyndp, tyndp_factors
    nb = _neighbour_model(config, fit_year)
    tyndp = load_tyndp(config.resolve(config.section("assumptions")["workbook"]))
    k = target_year - ref_year
    growth = {}
    for z in nb.load_coef:
        tf = tyndp_factors(tyndp, z, target_year, ref_year) if tyndp else None
        lg = tf["demand"] if (tf and tf.get("demand")) else (1 + _G_FALLBACK["demand"]) ** k
        rg = tf["res"] if (tf and tf.get("res")) else (1 + _G_FALLBACK["res"]) ** k
        growth[z] = (lg, rg)
    return all_weather_shapes(config, target_year, realization=draw, nb_model=nb, growth=growth)
