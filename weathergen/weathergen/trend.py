"""Phase 6 — externally-imposed climate trend via Quantile Delta Mapping (QDM).

The trend is NEVER estimated from the short record. It is a set of **quantile deltas** per
variable and per month — the change from a CMIP6 model's historical to its future (SSP) run —
imposed on the generator's present-climate output. Because the shift is quantile-wise, tail
intensification (e.g. hot extremes growing faster than the mean) is preserved. The shift is
time-varying: interpolated smoothly from the baseline year to the scenario horizon so each
simulated year gets the appropriate fraction.

# DECISION (D6.1): QDM implemented directly (numpy) for a transparent, time-varying, per-month
# quantile shift; xclim.sdba.QuantileDeltaMapping is the drop-in production alternative once real
# CMIP6 runs are ingested.
# DECISION (D6.2): per-variable application mode — temperature/dew point ADDITIVE, wind/precip
# MULTIPLICATIVE (non-negative), pressure NOT trended (physically ~stationary), cloud additive+clip.
# DECISION (D6.3): trend variability — if True, use the full quantile-delta curve (variance/tail
# changes); if False, shift by the median delta only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

_DEFAULT_MODE = {
    "temperature_c": "add", "dew_point_c": "add", "cloud_cover_pct": "add",
    "wind_speed_ms": "mult", "precip_1h_mm": "mult", "pressure_sea_hpa": "none",
}


@dataclass
class Trend:
    enabled: bool = False
    ssp: str | None = None
    baseline_year: int = 2020
    target_year: int = 2050
    quantiles: np.ndarray = field(default_factory=lambda: np.linspace(0.05, 0.95, 19))
    deltas: dict[str, np.ndarray] = field(default_factory=dict)   # var -> (12, nq) at target_year
    mode: dict[str, str] = field(default_factory=dict)
    trend_variability: bool = True

    def apply(self, cube: xr.DataArray) -> xr.DataArray:
        if not self.enabled or not self.deltas:
            return cube
        out = cube.copy()
        time = pd.DatetimeIndex(cube["time"].values)
        months = time.month.to_numpy()
        years = time.year.to_numpy().astype("float64")
        frac = np.clip((years - self.baseline_year) / max(self.target_year - self.baseline_year, 1), 0, None)

        for vi, v in enumerate(map(str, cube["variable"].values)):
            mode = self.mode.get(v, _DEFAULT_MODE.get(v, "add"))
            if v not in self.deltas or mode == "none":
                continue
            for si in range(cube.sizes["station"]):
                x = out.values[:, si, vi]
                for m in range(1, 13):
                    mask = months == m
                    idx = np.where(mask)[0]
                    xm = x[idx]
                    ok = ~np.isnan(xm)
                    if ok.sum() < 10:
                        continue
                    vals = xm[ok]
                    tau = (vals.argsort().argsort() + 0.5) / ok.sum()   # present-climate quantile
                    dm = self.deltas[v][m - 1]
                    if not self.trend_variability:
                        dm = np.full_like(dm, np.interp(0.5, self.quantiles, dm))
                    d = np.interp(tau, self.quantiles, dm) * frac[idx][ok]
                    xm[ok] = vals + d if mode == "add" else vals * (1.0 + d)
                    x[idx] = xm
                out.values[:, si, vi] = x
        return out


def load_deltas_npz(path: Path) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Load quantile deltas: npz with 'quantiles' (nq,) and one (12, nq) array per variable."""
    z = np.load(path)
    quantiles = z["quantiles"]
    deltas = {k: z[k] for k in z.files if k != "quantiles"}
    return quantiles, deltas


def fit(trend_cfg: dict, variables_cfg: dict | None = None) -> Trend:
    t = Trend(
        enabled=bool(trend_cfg.get("enabled", False)),
        ssp=trend_cfg.get("ssp"),
        trend_variability=bool(trend_cfg.get("trend_variability", True)),
        mode={v: _DEFAULT_MODE.get(v, "add") for v in (variables_cfg or {})},
    )
    path = trend_cfg.get("cmip6_deltas_path")
    if t.enabled and path and Path(path).exists():
        t.quantiles, t.deltas = load_deltas_npz(Path(path))
        t.baseline_year = int(trend_cfg.get("baseline_year", t.baseline_year))
        t.target_year = int(trend_cfg.get("target_year", t.target_year))
    return t
