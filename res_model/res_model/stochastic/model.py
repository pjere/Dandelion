"""Phase 5 — stochastic residual model for RES capacity factors (§5.4).

The calibrated deterministic chain under-disperses at hourly scale. This layer adds, per technology, a
residual with the right structure:
  * heteroscedastic by CF level — σ(CF) is largest mid-curve and → 0 at CF≈0 and CF≈rated (a wind
    turbine at zero or rated wind barely fluctuates; the steep mid-curve amplifies wind noise);
  * AR(1)–AR(2) temporal correlation (persistent forecast-error-like structure);
  * cross-technology contemporaneous correlation (a calm, clear anticyclone moves wind and PV residuals
    together) via a Cholesky factor;
  * bounded/beta-like CF marginal — the CF-dependent σ shrinking at the edges + clipping to [0, cf_max]
    keeps the noisy CF inside its physical range without a hard Gaussian tail.
Seeded and reproducible; consumed by the projection engine (Phase 6).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.signal import lfilter

from powersim_core.serialize import load_params, save_params


def _stabilise(phi: np.ndarray) -> np.ndarray:
    p = len(phi)
    for _ in range(50):
        comp = np.zeros((p, p)); comp[0] = phi
        if p > 1:
            comp[1:, :-1] = np.eye(p - 1)
        if np.max(np.abs(np.linalg.eigvals(comp))) < 0.999:
            return phi
        phi = phi * 0.95
    return phi


@dataclass
class ResidualModel:
    technologies: list[str]
    order: int
    bin_edges: np.ndarray                       # CF-level bin edges (heteroscedasticity)
    sigma: dict[str, np.ndarray]                # tech -> residual std per CF bin
    sigma_global: dict[str, float]              # tech -> fallback std
    phi: dict[str, np.ndarray]                  # tech -> AR coefficients
    innov_std: dict[str, float]                 # tech -> AR-innovation std (z has ~unit variance)
    corr: np.ndarray                            # cross-tech innovation-correlation matrix
    cf_max: float = 1.0
    metrics: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ helpers
    def _sigma_of(self, tech: str, cf: np.ndarray) -> np.ndarray:
        idx = np.clip(np.digitize(cf, self.bin_edges) - 1, 0, len(self.sigma[tech]) - 1)
        s = self.sigma[tech][idx].astype(float)
        s[np.isnan(s)] = self.sigma_global[tech]
        return s

    def simulate(self, modelled_cf: pd.DataFrame, n_paths: int = 1, seed: int | None = None,
                 rng: np.random.Generator | None = None) -> dict[str, pd.DataFrame]:
        """Add residual noise to a deterministic CF panel (index × technologies). Returns
        {tech: DataFrame(index × path)} of noisy CF, clipped to [0, cf_max]. Cross-tech correlated,
        AR-persistent, heteroscedastic. Pass `rng` (the F4 single-authority generator); `seed` is the
        legacy fallback."""
        rng = rng if rng is not None else np.random.default_rng(seed)
        techs = [t for t in self.technologies if t in modelled_cf.columns]
        pos = [self.technologies.index(t) for t in techs]
        sub = self.corr[np.ix_(pos, pos)]
        L = np.linalg.cholesky(sub + 1e-9 * np.eye(len(pos)))
        n = len(modelled_cf)
        out = {t: np.empty((n, n_paths)) for t in techs}
        for p in range(n_paths):
            E = rng.standard_normal((n, len(techs))) @ L.T          # cross-tech correlated white
            for j, t in enumerate(techs):
                innov = E[:, j] * self.innov_std[t]
                z = lfilter([1.0], np.r_[1.0, -self.phi[t]], innov)
                sig = self._sigma_of(t, modelled_cf[t].to_numpy())
                cf = modelled_cf[t].to_numpy() + z * sig
                out[t][:, p] = np.clip(cf, 0.0, self.cf_max)
        cols = [f"path_{i:03d}" for i in range(n_paths)]
        return {t: pd.DataFrame(out[t], index=modelled_cf.index, columns=cols) for t in techs}

    def save(self, path: str | Path) -> Path:
        """Portable JSON + npz sidecar for bin_edges/sigma/phi/corr arrays (no pickle — REVIEW F6)."""
        return save_params(asdict(self), Path(path).with_suffix(".json"))

    @staticmethod
    def load(path: str | Path) -> ResidualModel:
        return ResidualModel(**load_params(Path(path).with_suffix(".json")))


def _corr_from_chol(chol: np.ndarray) -> np.ndarray:
    return chol @ chol.T
