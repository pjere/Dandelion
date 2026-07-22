"""Step-vii price layer: the SMC→spot markup (the "wedge").

The dispatch LP returns the **system marginal cost** (SMC) — the dual of each zone's energy balance, i.e.
the SRMC of the marginal unit. Real day-ahead **spot** prices sit *above* SMC on average and are far more
volatile: generators bid above short-run marginal cost to recover start-up / no-load costs and scarcity
rents (unit commitment), peaks overshoot marginal cost as the system tightens, and surplus hours decouple
downward. This module fits that wedge — ``spot = SMC + markup(drivers)`` — on the backtest residual and
applies it to the projection SMC, so the trajectories are **spot forecasts, not marginal-cost curves**.

Design for projectability. The markup is a *transparent* regression on **structural** drivers that the
projection engine also produces — system tightness (residual demand / firm capacity), RES share, the SMC
level, and time-of-day / season shape — and **never** calendar-year effects (a 2019/2022 dummy cannot
extrapolate to 2040). An ordinary least squares on engineered features (not a black-box learner, which
would extrapolate wildly outside its training envelope) so the wedge degrades gracefully in the high-RES,
high-price 2040 regime the projection has to reach. Fit per zone — each market's microstructure differs.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

from .config import Config

# techs that count toward a zone's *firm* (dispatchable) capacity — the denominator of the tightness driver.
# Must-take RES / ROR / PSP are excluded (they're in res_pot, not firm); imports/DSR excluded (not local firm).
_FIRM = {"nuclear", "gas", "coal", "lignite", "oil", "biomass", "hydro_reservoir", "geothermal"}


def _feature_names() -> list[str]:
    return ["const", "smc", "tight", "tight_sq", "peak_kink", "res_share",
            "sin_h", "cos_h", "sin_2h", "cos_2h", "sin_m", "cos_m"]


def _ratios(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """The two clampable structural ratios: tightness (residual demand / firm capacity) and RES share."""
    firm = np.maximum(df["firm_cap"].to_numpy(float), 1.0)
    tight = np.clip((df["demand"].to_numpy(float) - df["musttake_res"].to_numpy(float)) / firm, 0.0, 1.6)
    res_share = np.clip(df["musttake_res"].to_numpy(float) / np.maximum(df["demand"].to_numpy(float), 1.0), 0, 1.5)
    return tight, res_share


def _features(df: pd.DataFrame, bounds: dict | None = None) -> np.ndarray:
    """Design matrix from a panel with columns [smc, demand, musttake_res, firm_cap, timestamp_utc].

    All drivers are quantities the projection engine also produces, so the fitted wedge is applicable
    forward. ``tight`` is the residual-demand ratio (load net of must-take RES over firm capacity); its
    square and a ``relu(tight-0.9)`` kink give the convex scarcity rent as the system approaches firm
    limits. ``res_share`` carries the downward surplus decoupling. Hour/month enter as cyclical harmonics
    (recurring, hence projectable) rather than dummies.

    ``bounds`` (from `fit_markup`) **clamps the structural ratios to their training envelope** before the
    design matrix is built: a 2040 system runs at RES shares far outside anything in 2019, and a linear term
    extrapolated there runs away — clamping holds the wedge flat beyond the observed range instead. ``smc`` is
    left un-clamped (a genuine price level that *should* extrapolate; ridge keeps its slope modest).
    """
    ts = pd.DatetimeIndex(df["timestamp_utc"])
    h = ts.hour.to_numpy(float)
    m = ts.month.to_numpy(float)
    tight, res_share = _ratios(df)
    if bounds is not None:
        tight = np.clip(tight, *bounds["tight"])
        res_share = np.clip(res_share, *bounds["res_share"])
    return np.column_stack([
        np.ones(len(df)), df["smc"].to_numpy(float), tight, tight ** 2,
        np.maximum(tight - 0.9, 0.0), res_share,
        np.sin(2 * np.pi * h / 24), np.cos(2 * np.pi * h / 24),
        np.sin(4 * np.pi * h / 24), np.cos(4 * np.pi * h / 24),
        np.sin(2 * np.pi * m / 12), np.cos(2 * np.pi * m / 12),
    ])


def _driver_bounds(df: pd.DataFrame) -> dict:
    """Training envelope (p1, p99) of the clampable structural ratios, so extrapolation is bounded."""
    tight, res_share = _ratios(df)
    return {"tight": [float(np.quantile(tight, 0.01)), float(np.quantile(tight, 0.99))],
            "res_share": [float(np.quantile(res_share, 0.01)), float(np.quantile(res_share, 0.99))]}


def zone_drivers(config: Config, year: int) -> dict[str, pd.DataFrame]:
    """Per-zone hourly [timestamp_utc, demand, musttake_res, firm_cap] for `year` — the projectable markup drivers.

    Recomputed from the same net-load + stack inputs the dispatch used (FR from ``load_fr_netload``,
    neighbours from ``neighbour_netload``), so fit-time and projection-time drivers are defined identically.
    ``firm_cap`` is the zone's dispatchable capacity (constant within a year; the tightness signal is the
    hourly residual demand moving against it).
    """
    from .io.fr_history import load_fr_netload
    from .neighbours.blocks import build_neighbour_stack, neighbour_netload
    from .rolling.windows import fr_stack_base

    zones = [z for z in config.all_zones if z != "GB"]
    out: dict[str, pd.DataFrame] = {}

    frs = fr_stack_base(config, year)
    fr_firm = float(frs.loc[frs["tech"].isin(_FIRM), "capacity_mw"].sum())
    fr = load_fr_netload(config, f"{year}-01-01", f"{year + 1}-01-01")
    out["FR"] = pd.DataFrame({"timestamp_utc": pd.to_datetime(fr["timestamp_utc"], utc=True),
                              "demand": fr["demand_mw"].to_numpy(float),
                              "musttake_res": fr["musttake_res_mw"].to_numpy(float), "firm_cap": fr_firm})
    for z in zones:
        if z == "FR":
            continue
        try:                                    # virtual export-sink zones (DE_REST) can lack a full stack in
            st = build_neighbour_stack(config, z, year)   # some years — they carry no observed spot, so the
        except (KeyError, ValueError):          # markup fit skips them regardless; just omit the drivers.
            continue
        firm = float(st.loc[st["tech"].isin(_FIRM), "capacity_mw"].sum())
        nl = neighbour_netload(config, z, year)
        out[z] = pd.DataFrame({"timestamp_utc": pd.to_datetime(nl["timestamp_utc"], utc=True),
                               "demand": nl["load_mw"].to_numpy(float),
                               "musttake_res": nl["musttake_res_mw"].to_numpy(float), "firm_cap": firm})
    return out


def _year_smc(config: Config, year: int) -> pd.DataFrame:
    """This year's model SMC per zone — from the saved lake backtest_prices if present (fast), else by
    running the backtest. Fitting the wedge should not silently trigger a 20-minute re-solve."""
    from powersim_core import lake
    try:
        m = lake.read_table("dispatch", "backtest_prices", year=year)
        return m if not m.empty else _run_smc(config, year)
    except (FileNotFoundError, ValueError):
        return _run_smc(config, year)


def _run_smc(config: Config, year: int) -> pd.DataFrame:
    from .rolling.backtest import run_backtest
    return run_backtest(config, year)["model_prices"]


def build_panel(config: Config, years: list[int], max_median_ratio: float = 1.8,
                min_corr: float = 0.2) -> pd.DataFrame:
    """Long training panel [zone, timestamp_utc, smc, observed, demand, musttake_res, firm_cap] across `years`.

    Uses each year's saved backtest SMC (re-solving only if not on the lake), joins observed spot and the
    projectable drivers, and stacks the zones. The markup target is ``observed − smc``.

    **Calibration-quality gate.** A zone-year the dispatch prices badly is a *failed dispatch*, not a wedge
    the markup should learn, so it is dropped — on either symptom: (a) gross level error (median outside
    [1/`max_median_ratio`, `max_median_ratio`] × observed median), or (b) wrong shape (SMC↔spot correlation
    below `min_corr`). CH/IT-North in the 2022 drought / nuclear-crisis year fail (b) — level ≈ OK but
    correlation ≈ 0 — while FR/DE-LU/BE/ES keep their crisis-price signal. So a crisis year still contributes
    its clean zones without the broken ones poisoning the fit."""
    from .rolling.backtest import _observed_prices

    frames = []
    zones = [z for z in config.all_zones if z != "GB"]
    for y in years:
        model = _year_smc(config, y)
        obs = _observed_prices(config, y, zones)
        drv = zone_drivers(config, y)
        for z, dz in drv.items():
            if z not in model.columns or obs.get(z) is None:
                continue
            d = dz.set_index("timestamp_utc")
            smc = model[z].reindex(d.index)
            o = obs[z].reindex(d.index)
            om, sm = float(o.median()), float(smc.median())
            if om > 0 and not (1.0 / max_median_ratio <= sm / om <= max_median_ratio):
                continue                                    # (a) gross level error → excluded
            ok = smc.notna() & o.notna()
            if ok.sum() < 100 or float(np.corrcoef(smc[ok], o[ok])[0, 1]) < min_corr:
                continue                                    # (b) wrong shape (SMC↔spot corr too low) → excluded
            f = d.assign(zone=z, smc=smc.to_numpy(), observed=o.to_numpy()).reset_index()
            frames.append(f.dropna(subset=["smc", "observed"]))
    panel = pd.concat(frames, ignore_index=True)
    return panel[["zone", "timestamp_utc", "smc", "observed", "demand", "musttake_res", "firm_cap"]]


# Economic sign constraints on the standardized coefficients. These are what make the wedge *projectable*:
# 2019 alone cannot pin down the 2040 driver combination (high price AND low tightness — a pairing the
# training year never contains), so unconstrained fits happily extrapolate an economically absurd wedge that
# *shrinks* as prices rise (a plain fit gave IT-North −€68 in 2030). Requiring markup to be non-decreasing in
# the price level and in tightness encodes the economics the data alone can't, and bounds the extrapolation.
_SIGN_LB = {"smc": 0.0, "tight": 0.0, "peak_kink": 0.0}        # others unconstrained


def _ridge(X: np.ndarray, y: np.ndarray, alpha: float, names: list[str]
           ) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """Sign-constrained ridge on *standardized* features (intercept via y-centering, unpenalized).

    Standardizing first means one α penalizes every driver on a common scale; ridge tames the tight↔smc
    collinearity that made a plain fit split their effect into huge offsetting coefficients. The ridge is
    imposed by *augmenting* the system ([Z; √α·I] vs [y−ȳ; 0]) so it can be solved as a bounded least squares
    (`scipy.optimize.lsq_linear`) with the `_SIGN_LB` economic constraints. Returns (mu, sd, beta_z, ybar) so
    `apply` reproduces prediction = ȳ + ((x−mu)/sd)·beta_z.
    """
    from scipy.optimize import lsq_linear

    Z0 = X[:, 1:]                                              # drop the const column (handled by ybar)
    mu, sd = Z0.mean(0), Z0.std(0) + 1e-9
    Z = (Z0 - mu) / sd
    ybar = float(y.mean())
    p = Z.shape[1]
    Z_aug = np.vstack([Z, np.sqrt(alpha) * np.eye(p)])         # ridge as extra rows → bounded LSQ
    y_aug = np.concatenate([y - ybar, np.zeros(p)])
    feat = names[1:]                                           # names aligned to Z columns
    lb = np.array([_SIGN_LB.get(f, -np.inf) for f in feat])
    beta_z = lsq_linear(Z_aug, y_aug, bounds=(lb, np.full(p, np.inf))).x
    return mu, sd, beta_z, ybar


def _predict(model_z: dict, X: np.ndarray) -> np.ndarray:
    mu, sd, beta_z, ybar = (np.array(model_z["mu"]), np.array(model_z["sd"]),
                            np.array(model_z["beta_z"]), model_z["ybar"])
    return ybar + ((X[:, 1:] - mu) / sd) @ beta_z


def fit_markup(panel: pd.DataFrame, alpha_frac: float = 0.1, shrink: float = 0.5) -> dict:
    """Per-zone **ridge** fit of the wedge (observed − smc) on the (standardized, envelope-clamped) structural
    features. `alpha_frac` sets the ridge penalty as a fraction of n (≈ the standardized ZᵀZ diagonal), so it
    scales with sample size. Returns a serializable per-zone model {mu, sd, beta_z, ybar, bounds} plus
    in-sample diagnostics (RMSE / R² of spot vs raw SMC — the markup must *reduce* price error).

    **`shrink` (default 0.5) scales the whole fitted wedge toward zero** — the level *and* the driver slopes.
    Rationale, measured out-of-sample (fit 2019+2022, predict 2023/2024): the full wedge (shrink=1) is
    calibrated across a normal year and the 2022 crisis, so its mean ≈ +40 €/MWh; applied to the now much
    better SMC (nuclear supply curve + structural water value cut the residual gap), that full wedge
    *overshoots* the smaller 2023/2024 gap — MAE 26,3 → 31,2, correlation down. Halving it keeps MAE at the
    raw-SMC level while still cutting the baseload error roughly in half (|baseload| 14,6/24,2 → 6,2/15,3 %).
    Regime-aware *reparametrisations* (a wedge proportional to SMC) were tried and were worse on MAE — the
    ridge already carries the regime through its SMC/tightness features; the only defect was that it was too
    big. So the lever is shrinkage, not reparametrisation. `shrink=1.0` recovers the un-shrunk fit.
    """
    names = _feature_names()
    coefs, diag = {}, {}
    for z, g in panel.groupby("zone"):
        bounds = _driver_bounds(g)
        X = _features(g, bounds)
        y = (g["observed"] - g["smc"]).to_numpy(float)         # the wedge
        mu, sd, beta_z, ybar = _ridge(X, y, alpha=alpha_frac * len(g), names=names)
        beta_z, ybar = shrink * beta_z, shrink * ybar          # rétrécissement du wedge entier (cf. docstring)
        mz = {"mu": mu.tolist(), "sd": sd.tolist(), "beta_z": beta_z.tolist(), "ybar": ybar, "bounds": bounds}
        markup = _predict(mz, X)                               # already shrunk
        smc = g["smc"].to_numpy(float)
        obs = g["observed"].to_numpy(float)
        pred_spot = smc + markup
        rmse_smc = float(np.sqrt(np.mean((smc - obs) ** 2)))
        rmse_spot = float(np.sqrt(np.mean((pred_spot - obs) ** 2)))
        sst = float(np.sum((obs - obs.mean()) ** 2))
        coefs[z] = mz
        diag[z] = {"n": int(len(g)), "rmse_smc": round(rmse_smc, 2), "rmse_spot": round(rmse_spot, 2),
                   "r2_spot": round(1 - np.sum((pred_spot - obs) ** 2) / sst, 3) if sst > 0 else np.nan,
                   "mean_markup": round(float(markup.mean()), 2)}
    return {"features": names, "coef": coefs, "diagnostics": diag, "alpha_frac": alpha_frac,
            "shrink": shrink, "years": sorted(panel["timestamp_utc"].dt.year.unique().tolist())}


def apply_markup(model: dict, zone: str, smc: pd.Series, drivers: pd.DataFrame,
                 floor: float = -500.0, voll: float = 4000.0) -> pd.Series:
    """spot = SMC + markup(drivers), clipped to [floor, voll]. `drivers` has [timestamp_utc, demand, musttake_res,
    firm_cap] aligned to `smc`; falls back to clipped SMC for a zone the model never saw. Drivers are clamped
    to the training envelope (`bounds`) so the wedge holds flat rather than extrapolating wildly."""
    mz = model["coef"].get(zone)
    if mz is None:
        return smc.clip(floor, voll)
    df = drivers.assign(smc=smc.to_numpy(float))
    X = _features(df, mz.get("bounds"))
    return pd.Series(np.clip(smc.to_numpy(float) + _predict(mz, X), floor, voll), index=smc.index)


def save_model(config: Config, model: dict) -> str:
    p = config.reports_dir / "markup_model.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(model, indent=2))
    return str(p)


def load_model(config: Config) -> dict:
    return json.loads((config.reports_dir / "markup_model.json").read_text())


# ---------------------------------------------------------------------------------------------------
# Variante monotone : corriger le niveau sans toucher à l'ordre des heures
# ---------------------------------------------------------------------------------------------------
# Le wedge par régression horaire corrige bien le niveau mais déforme la structure : mesuré sur FR 2024,
# le taux de capture solaire passe de 0,697 (LP brut, contre 0,676 observé) à 1,104 — un taux supérieur à
# 1 signifierait que le solaire produit aux heures chères, l'inverse de la cannibalisation réelle. Il
# triple aussi la MAE en 2019 (8,3 -> 22,9) tout en aidant en 2022 : un wedge unique ne peut pas servir
# des régimes dont l'écart varie d'un facteur cinq.
#
# Ici le wedge est une fonction **monotone du seul SMC**, ajustée par appariement de quantiles. Trois
# propriétés en découlent :
#   - l'ordre des heures est préservé exactement, donc la structure horaire du LP — qui est bonne — n'est
#     pas abîmée ;
#   - le niveau ET la forme de la distribution (négatifs, pointes) sont corrigés par construction ;
#   - c'est une fonction pure du SMC, donc applicable en projection : quand la distribution des SMC se
#     déplace avec la pénétration RES, l'image se déplace avec elle.
# Le prix est la perte de la logique de rareté par driver (tightness) — que les mesures désignent
# justement comme nuisible.
N_QUANTILES = 199


def fit_markup_monotone(panel: pd.DataFrame, n_q: int = N_QUANTILES) -> dict:
    """Appariement de quantiles SMC → spot par zone. Renvoie les nœuds d'une interpolation monotone."""
    out: dict[str, dict] = {}
    qs = np.linspace(0.0, 1.0, n_q)
    for z, g in panel.groupby("zone"):
        s = pd.to_numeric(g["smc"], errors="coerce")
        o = pd.to_numeric(g["observed"], errors="coerce")
        ok = s.notna() & o.notna()
        if ok.sum() < 200:
            continue
        xs = np.quantile(s[ok].to_numpy(float), qs)
        ys = np.quantile(o[ok].to_numpy(float), qs)
        # quantiles empiriques déjà triés ; on force la stricte croissance en x pour que np.interp
        # reste bien défini quand le SMC est plat (FR est à 7 EUR/MWh sur la moitié de l'année)
        xs = np.maximum.accumulate(xs + np.arange(len(xs)) * 1e-9)
        out[z] = {"x": xs.tolist(), "y": np.maximum.accumulate(ys).tolist()}
    return {"kind": "monotone", "nodes": out, "n_q": int(n_q)}


def apply_markup_monotone(model: dict, zone: str, smc: pd.Series,
                          floor: float = -500.0, voll: float = 4000.0) -> pd.Series:
    """Applique la carte monotone. Hors des nœuds, on prolonge par décalage constant plutôt que par
    extrapolation linéaire : un SMC 2046 très au-delà de l'historique ne doit pas être amplifié."""
    nd = model.get("nodes", {}).get(zone)
    v = smc.to_numpy(float)
    if not nd:
        return smc.clip(floor, voll)
    x, y = np.asarray(nd["x"], float), np.asarray(nd["y"], float)
    out = np.interp(v, x, y, left=np.nan, right=np.nan)
    lo, hi = v < x[0], v > x[-1]
    out[lo] = v[lo] + (y[0] - x[0])
    out[hi] = v[hi] + (y[-1] - x[-1])
    return pd.Series(np.clip(out, floor, voll), index=smc.index)
