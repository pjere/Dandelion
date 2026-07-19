"""Phase 5 — fit the stochastic residual model from historical residuals (observed − calibrated CF)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import Config
from .model import ResidualModel, _stabilise


def historical_calibrated_cf(config: Config, cal) -> pd.DataFrame:
    """Deterministic calibrated CF over history, per technology (what the residual is defined against)."""
    from ..calibration.historical import hist_drivers, modelled_hist_cf
    mod = modelled_hist_cf(config)
    drivers = hist_drivers(config)
    w100 = drivers["w100_nat"].reindex(mod.index)
    out = {"pv": cal.apply_pv(mod["pv"]),
           "wind_onshore": cal.apply_onshore(w100),
           "hydro_ror": cal.apply_hydro(drivers["precip_nat"].reindex(mod.index),
                                        drivers["temp_nat"].reindex(mod.index))}
    if "wind_offshore" in mod:                       # time-varying-fleet modelled × calibrated scale
        out["wind_offshore"] = (mod["wind_offshore"] * cal.offshore["cf_scale"]).clip(0.0, 1.0)
    return pd.DataFrame(out)


def _fit_ar(z: pd.Series, order: int) -> tuple[np.ndarray, pd.Series]:
    """OLS AR(order) on a gappy hourly standardised series → (phi, innovation series on the grid)."""
    grid = z.asfreq("h")
    lags = pd.concat({k: grid.shift(k) for k in range(order + 1)}, axis=1)
    lags.columns = list(range(order + 1))
    ok = lags.notna().all(axis=1)
    Y = lags.loc[ok, 0].to_numpy()
    X = lags.loc[ok, list(range(1, order + 1))].to_numpy()
    phi = _stabilise(np.linalg.lstsq(X, Y, rcond=None)[0])
    eta = pd.Series(np.nan, index=grid.index)
    eta.loc[ok] = Y - X @ phi
    return phi, eta


def fit_residual_model(config: Config, cal, order: int = 2, n_bins: int = 12) -> ResidualModel:
    from ..calibration.fit import _observed_cf
    det = historical_calibrated_cf(config, cal)
    obs = _observed_cf(config)
    cf_max = 1.0
    edges = np.linspace(0.0, cf_max, n_bins + 1)
    techs = [t for t in det.columns if t in obs.columns]

    sigma, sigma_global, phi, innov_std, eta_cols, metrics = {}, {}, {}, {}, {}, {}
    for t in techs:
        d = pd.concat([det[t].rename("m"), obs[t].rename("o")], axis=1).dropna().sort_index()
        e = d["o"] - d["m"]
        # heteroscedastic σ by CF level
        b = np.clip(np.digitize(d["m"].to_numpy(), edges) - 1, 0, n_bins - 1)
        s = np.array([e.to_numpy()[b == k].std() if (b == k).sum() > 30 else np.nan
                      for k in range(n_bins)])
        sigma_global[t] = float(np.nanstd(e.to_numpy()))
        s = np.where(np.isnan(s), sigma_global[t], s)
        sigma[t] = s
        # standardise then AR fit
        sig_t = s[b]; sig_t[sig_t <= 0] = sigma_global[t]
        z = pd.Series(e.to_numpy() / sig_t, index=d.index)
        phi[t], eta = _fit_ar(z, order)
        innov_std[t] = float(np.nanstd(eta.to_numpy()))
        eta_cols[t] = eta
        # heteroscedasticity signature: mid-CF σ vs edge σ
        mid = np.nanmean(s[n_bins // 3: 2 * n_bins // 3]); edge = np.nanmean([s[0], s[-1]])
        metrics[f"{t}_sigma_mid_over_edge"] = round(float(mid / edge), 2) if edge > 0 else np.nan
        metrics[f"{t}_resid_std"] = round(sigma_global[t], 4)
        metrics[f"{t}_phi"] = [round(float(x), 3) for x in phi[t]]

    # cross-technology innovation correlation
    E = pd.DataFrame(eta_cols)
    corr = E.corr().reindex(index=techs, columns=techs).to_numpy()
    corr = np.nan_to_num(corr, nan=0.0); np.fill_diagonal(corr, 1.0)
    corr = 0.98 * corr + 0.02 * np.eye(len(techs))
    metrics["cross_tech_corr"] = {f"{techs[i]}~{techs[j]}": round(float(corr[i, j]), 2)
                                  for i in range(len(techs)) for j in range(i + 1, len(techs))}

    rm = ResidualModel(technologies=techs, order=order, bin_edges=edges, sigma=sigma,
                       sigma_global=sigma_global, phi=phi, innov_std=innov_std, corr=corr,
                       cf_max=cf_max, metrics=metrics)
    # self-check: simulated residual std reproduces empirical, on the historical deterministic CF
    sim = rm.simulate(det[techs].dropna(), n_paths=1, seed=config.seed)
    for t in techs:
        emp = float((obs[t] - det[t]).std())
        got = float((sim[t]["path_000"] - det[t].reindex(sim[t].index)).std())
        metrics[f"{t}_sim_resid_std"] = round(got, 4)
        metrics[f"{t}_std_ratio"] = round(got / emp, 3) if emp > 0 else np.nan
    return rm
