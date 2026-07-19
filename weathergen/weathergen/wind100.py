"""Co-generation of ERA5 100 m wind conditioned on the simulated 10 m station wind.

Step (iv) flagged that a *deterministic* station-10 m→100 m transfer is a materially weaker hourly
predictor than ERA5-100 m (it compresses variance and loses spatial coherence). The clean fix — per
the step-(iv) spec §2.A — is to co-generate 100 m wind here in step (ii), so the renewables model reads
a physically-realistic 100 m field that stays coherent with the rest of the weather draw.

Per station s:  log(w100) = a_s + b_s·log(w10) + c_s·temp_anom + r_s(t)
with r_s a **spatially-correlated AR(1)** residual — AR(1) gives realistic ramps, the spatial Cholesky
keeps a low-wind lull coherent across France (the Dunkelflaute driver). Fitted on historical
(SYNOP 10 m, ERA5 100 m) pairs; applied to the simulated 10 m wind with the same seeded rng.

The **temperature term** (#79) is the fix for the winter demand–RES correlation gap: 100 m wind couples to
temperature *more* strongly than 10 m (it is more geostrophic/synoptic), but a transfer that routes all
temp coupling through 10 m — plus a temp-independent residual that is 65 % of the variance — makes the
co-generated 100 m wind *less* temp-coupled than 10 m, inverting reality (see weathergen/WIND_TEMP_COUPLING.md).
Adding the **deseasonalized** temperature anomaly as a predictor (c_s > 0: a mild *within-winter* anomaly ⇒
windier — Atlantic low-pressure storms are both mild and windy) restores the direct synoptic coupling the
transfer alone cannot carry. Deseasonalizing matters: the raw temp↔wind sign flips across seasons (calm hot
summers), so only the within-season anomaly carries the winter coupling. Models fitted before this (c=None)
fall back to the transfer-only mean.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr
from scipy.signal import lfilter

from powersim_core.serialize import load_params, save_params

_EPS = 0.1
VAR_NAME = "wind_speed_100m_ms"


def _doy_design(index: pd.DatetimeIndex) -> np.ndarray:
    """Day-of-year harmonic design [1, cos1, sin1, cos2, sin2] for a seasonal temperature climatology.
    Deseasonalizing is essential (#79): the temp↔wind coupling flips sign between seasons (hot summer =
    calm high pressure vs mild winter = windy Atlantic storms), so only the *within-season* anomaly carries
    the winter coupling the demand–RES correlation needs — raw temperature fits the wrong cross-seasonal sign."""
    doy = pd.DatetimeIndex(index).dayofyear.to_numpy(float)
    w = 2 * np.pi * doy / 365.25
    return np.column_stack([np.ones(len(doy)), np.cos(w), np.sin(w), np.cos(2 * w), np.sin(2 * w)])


def _temp_anomaly(temp: np.ndarray, index: pd.DatetimeIndex, seas: np.ndarray) -> np.ndarray:
    """Deseasonalized temperature anomaly = temp − climatology(day-of-year), given harmonic coeffs `seas`."""
    return temp - _doy_design(index) @ seas


@dataclass
class Wind100Model:
    station_ids: list[str]
    a: np.ndarray                   # intercept per station
    b: np.ndarray                   # log-slope (≈ shear) per station
    phi: np.ndarray                 # AR(1) coefficient per station
    sigma: np.ndarray               # stationary residual std (log space) per station
    chol: np.ndarray                # Cholesky of the cross-station residual correlation
    fit_r2: np.ndarray              # per-station transfer R²
    c: np.ndarray | None = None            # temp-anomaly coefficient per station (#79); None → no temp term
    temp_seas: np.ndarray | None = None    # per-station day-of-year climatology harmonic coeffs (S×5)
    temp_astd: np.ndarray | None = None    # per-station std of the DESEASONALIZED temp anomaly

    def append(self, cube: xr.DataArray, rng: np.random.Generator) -> xr.DataArray:
        """Append the co-generated 100 m wind variable to a simulated cube."""
        if VAR_NAME in set(map(str, cube["variable"].values)):
            return cube
        w10 = cube.sel(variable="wind_speed_ms").transpose("time", "station")
        order = [str(s) for s in w10["station"].values]
        if any(s not in self.station_ids for s in order):               # station set ≠ fitted model
            return cube                                                 # (e.g. synthetic smoke test) → skip
        pos = [self.station_ids.index(s) for s in order]                # align model → cube order
        a, b, phi, sigma = self.a[pos], self.b[pos], self.phi[pos], self.sigma[pos]
        L = self.chol[np.ix_(pos, pos)]

        n, S = w10.shape
        mean = a[None, :] + b[None, :] * np.log(np.clip(w10.values, 0, None) + _EPS)
        # #79: direct synoptic temp coupling — add c·(deseasonalized temp anomaly) where the model was fitted
        # with it and the cube carries temperature. The anomaly is temp minus the fit-time day-of-year
        # climatology, so it captures the *within-winter* mild-and-windy coupling (not the cross-seasonal sign).
        if self.c is not None and "temperature_c" in set(map(str, cube["variable"].values)):
            temp = cube.sel(variable="temperature_c").transpose("time", "station").sel(station=w10["station"]).values
            seas = np.asarray(self.temp_seas)[pos]                        # (S, K) climatology coeffs
            clim = _doy_design(pd.DatetimeIndex(w10["time"].values)) @ seas.T    # (n, S)
            astd = np.where(np.asarray(self.temp_astd)[pos] == 0, 1.0, np.asarray(self.temp_astd)[pos])
            temp_anom = (temp - clim) / astd[None, :]
            mean = mean + np.asarray(self.c)[pos][None, :] * temp_anom
        # spatially-correlated AR(1) residual field (stationary std = sigma)
        innov = (rng.standard_normal((n, S)) @ L.T) * (sigma * np.sqrt(1.0 - phi ** 2))[None, :]
        resid = np.empty_like(innov)
        for j in range(S):
            resid[:, j] = lfilter([1.0], [1.0, -phi[j]], innov[:, j])
        w100 = np.clip(np.exp(mean + resid) - _EPS, 0.0, None)

        da = xr.DataArray(w100[:, :, None], dims=("time", "station", "variable"),
                          coords={"time": cube["time"], "station": w10["station"],
                                  "variable": [VAR_NAME]})
        for c in ("latitude", "longitude", "elevation", "lst_offset_h"):
            if c in cube.coords:
                da = da.assign_coords({c: ("station", cube[c].sel(station=w10["station"]).values)})
        return xr.concat([cube, da.transpose("time", "station", "variable")], dim="variable")

    def save(self, path: str | Path) -> Path:
        """Portable JSON + npz sidecar for the per-station arrays (no pickle — REVIEW F6)."""
        return save_params(asdict(self), Path(path).with_suffix(".json"))

    @staticmethod
    def load(path: str | Path) -> Wind100Model:
        return Wind100Model(**load_params(Path(path).with_suffix(".json")))


def fit_wind100(w10_panel: pd.DataFrame, w100_panel: pd.DataFrame,
                temp_panel: pd.DataFrame | None = None) -> Wind100Model:
    """Fit the conditional model from aligned (time × station) 10 m and 100 m wind panels.

    If `temp_panel` (station temperature °C) is given, the mean gains a standardized-temperature term
    (#79): ``log(w100) = a + b·log(w10) + c·(temp − temp_mean)/temp_std + r`` — restoring the synoptic
    temp↔wind coupling the transfer alone loses. Without it the fit is the transfer-only model as before."""
    stations = [str(c) for c in w10_panel.columns if c in w100_panel.columns]
    w10_panel, w100_panel = w10_panel[stations], w100_panel[stations]
    idx = pd.DatetimeIndex(w10_panel.index)
    design = _doy_design(idx)                                # day-of-year harmonics for deseasonalization
    a, b, c, phi, sigma, r2, seas, astd = ([] for _ in range(8))
    resid_cols = {}
    for s in stations:
        x = np.log(w10_panel[s].to_numpy() + _EPS)
        y = np.log(w100_panel[s].to_numpy() + _EPS)
        has_temp = temp_panel is not None and s in temp_panel.columns
        if has_temp:
            t = pd.to_numeric(temp_panel[s], errors="coerce").to_numpy(float)
            tok = np.isfinite(t)
            scoef, *_ = np.linalg.lstsq(design[tok], t[tok], rcond=None)   # seasonal climatology
            anom = t - design @ scoef                          # within-season temperature anomaly
            asd = float(np.nanstd(anom)) or 1.0
            ta = anom / asd
            ok = np.isfinite(x) & np.isfinite(y) & np.isfinite(ta)
            cols = [np.ones(ok.sum()), x[ok], ta[ok]]
        else:
            scoef, asd = np.zeros(design.shape[1]), 1.0
            ok = np.isfinite(x) & np.isfinite(y)
            cols = [np.ones(ok.sum()), x[ok]]
        X = np.column_stack(cols)
        coef, *_ = np.linalg.lstsq(X, y[ok], rcond=None)
        r = np.full(len(x), np.nan)
        r[ok] = y[ok] - X @ coef
        ss = float(np.nansum(r[ok] ** 2)); tot = float(np.nansum((y[ok] - y[ok].mean()) ** 2))
        a.append(coef[0]); b.append(coef[1]); c.append(coef[2] if has_temp else 0.0)
        seas.append(scoef); astd.append(asd)
        r2.append(1 - ss / tot if tot > 0 else 0.0)
        rr = pd.Series(r).dropna()
        phi.append(float(rr.autocorr(1)) if len(rr) > 10 else 0.0)
        sigma.append(float(rr.std()))
        resid_cols[s] = pd.Series(r, index=w10_panel.index)
    phi = np.clip(np.array(phi), -0.99, 0.99)
    # cross-station residual correlation → Cholesky (PD-regularised)
    R = pd.DataFrame(resid_cols).corr().to_numpy()
    R = np.nan_to_num(R, nan=0.0); np.fill_diagonal(R, 1.0)
    R = 0.99 * R + 0.01 * np.eye(len(stations))
    chol = np.linalg.cholesky(R)
    temp_fields = ({"c": np.array(c), "temp_seas": np.array(seas), "temp_astd": np.array(astd)}
                   if temp_panel is not None else {})            # no temp → identical to the transfer-only model
    return Wind100Model(station_ids=stations, a=np.array(a), b=np.array(b), phi=phi,
                        sigma=np.array(sigma), chol=chol, fit_r2=np.array(r2), **temp_fields)
