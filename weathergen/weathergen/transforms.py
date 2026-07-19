"""Phase 3 — per-variable transforms toward an unbounded ~Gaussian latent.

# DECISION (D3.1): transforms are applied to the RAW variable BEFORE climatology
# (transform -> deseasonalize), generalizing the spec's "clear-sky-index then
# deseasonalize" ordering. A logit (bounded) or hurdle (intermittent) is only meaningful
# on the raw value, not on a standardized anomaly. Identity-transformed variables
# (temperature, pressure, dew point) are unaffected, so their Phase-2 result is unchanged.

Per-variable maps (fit pooled across stations; the station-level mean/variance is handled
by the climatology):
  * gaussian (temp, pressure, dew point): identity, or light Yeo-Johnson if |skew|>0.5
  * positive_skew (wind): Yeo-Johnson (signed-safe; calms=0 preserved, their mass handled
    by the Phase-4 marginal)
  * bounded_01 (cloud, humidity): logit on (x-lo)/(hi-lo)
  * intermittent (precip): log1p (monotone, 0->0); the wet/dry OCCURRENCE is deferred to the
    Phase-4 censored/hurdle marginal

Every map provides exact ``forward``/``inverse`` (inverse(forward(x)) == x to numerical tol,
except saturating clips at logit bounds, which are flagged).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np
import xarray as xr

_EPS = 1e-6


class _Map(Protocol):
    def forward(self, x: np.ndarray) -> np.ndarray: ...
    def inverse(self, y: np.ndarray) -> np.ndarray: ...


@dataclass
class Identity:
    def forward(self, x): return x
    def inverse(self, y): return y


@dataclass
class YeoJohnson:
    lmbda: float

    def forward(self, x):
        x = np.asarray(x, dtype="float64")
        out = np.empty_like(x)
        pos = x >= 0
        l = self.lmbda
        with np.errstate(all="ignore"):
            if abs(l) < 1e-8:
                out[pos] = np.log1p(x[pos])
            else:
                out[pos] = (np.power(x[pos] + 1.0, l) - 1.0) / l
            if abs(l - 2.0) < 1e-8:
                out[~pos] = -np.log1p(-x[~pos])
            else:
                out[~pos] = -(np.power(-x[~pos] + 1.0, 2.0 - l) - 1.0) / (2.0 - l)
        return out

    def inverse(self, y):
        y = np.asarray(y, dtype="float64")
        out = np.empty_like(y)
        pos = y >= 0
        l = self.lmbda
        with np.errstate(all="ignore"):
            if abs(l) < 1e-8:
                out[pos] = np.expm1(y[pos])
            else:
                base = np.maximum(y[pos] * l + 1.0, 1e-12)
                out[pos] = np.power(base, 1.0 / l) - 1.0
            if abs(l - 2.0) < 1e-8:
                out[~pos] = 1.0 - np.exp(-y[~pos])
            else:
                base = np.maximum(1.0 - (2.0 - l) * y[~pos], 1e-12)
                out[~pos] = 1.0 - np.power(base, 1.0 / (2.0 - l))
        return out


@dataclass
class Logit:
    lo: float
    hi: float

    def forward(self, x):
        p = np.clip((np.asarray(x, dtype="float64") - self.lo) / (self.hi - self.lo), _EPS, 1 - _EPS)
        return np.log(p / (1 - p))

    def inverse(self, y):
        p = 1.0 / (1.0 + np.exp(-np.clip(np.asarray(y, dtype="float64"), -50, 50)))
        return self.lo + (self.hi - self.lo) * p


@dataclass
class Log1p:
    def forward(self, x):
        return np.log1p(np.clip(np.asarray(x, dtype="float64"), 0, None))

    def inverse(self, y):
        # clip in log-space to avoid overflow; physical bounds are enforced downstream
        return np.expm1(np.clip(np.asarray(y, dtype="float64"), -50, 50))


@dataclass
class TransformSet:
    maps: dict[str, _Map] = field(default_factory=dict)

    def _apply(self, cube: xr.DataArray, fn: str) -> xr.DataArray:
        out = cube.copy()
        for vi, v in enumerate(map(str, cube["variable"].values)):
            m = self.maps.get(v, Identity())
            x = cube.values[:, :, vi]
            ok = ~np.isnan(x)
            res = x.copy()
            res[ok] = getattr(m, fn)(x[ok])
            out.values[:, :, vi] = res
        return out

    def forward(self, cube: xr.DataArray) -> xr.DataArray:
        return self._apply(cube, "forward")

    def inverse(self, latent: xr.DataArray) -> xr.DataArray:
        return self._apply(latent, "inverse")


def _fit_one(values: np.ndarray, kind: str, bounds: tuple[float, float]) -> _Map:
    from scipy import stats

    v = values[~np.isnan(values)]
    if v.size > 50000:
        v = np.random.default_rng(0).choice(v, 50000, replace=False)
    if kind == "bounded_01":
        return Logit(float(bounds[0]), float(bounds[1]))
    if kind == "intermittent":
        return Log1p()
    if kind == "positive_skew":
        _, lmbda = stats.yeojohnson(v)
        return YeoJohnson(float(lmbda))
    # gaussian: only transform if meaningfully skewed
    if v.size > 10 and abs(float(stats.skew(v))) > 0.5:
        _, lmbda = stats.yeojohnson(v)
        return YeoJohnson(float(lmbda))
    return Identity()


def fit(cube: xr.DataArray, variables_cfg: dict[str, dict]) -> TransformSet:
    maps: dict[str, _Map] = {}
    for vi, v in enumerate(map(str, cube["variable"].values)):
        cfg = variables_cfg.get(v, {})
        maps[v] = _fit_one(cube.values[:, :, vi].ravel(), cfg.get("kind", "gaussian"),
                           tuple(cfg.get("bounds", (0.0, 1.0))))
    return maps_to_set(maps)


def maps_to_set(maps: dict[str, _Map]) -> TransformSet:
    return TransformSet(maps=maps)
