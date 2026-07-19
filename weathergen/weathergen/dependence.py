"""Phase 5 — spatial + cross-variable + temporal dependence on the Gaussian latent field.

The Phase-4 PIT has already mapped each location (station x variable) to a standard-normal
latent column. Here we model their joint space-variable-time structure with one coherent,
auditable reduced model:

  1. EOF/PCA reduction of the S*V latent field to k leading modes (``eof_variance``).
  2. VAR(p) on the PC scores  ->  couples space, variables and time (autocorrelation to many
     lags, cross-variable and inter-station correlation via the shared modes).
  3. Reconstruct the full latent field from the modes, adding back the discarded-mode variance
     as per-dimension residual noise so each latent column stays ~N(0,1) (required by the Φ PIT).

# DECISION (D5.1): Gaussian copula (VAR with Gaussian innovations) — marginal-consistent with Φ PIT.
# DECISION (D5.2): reduction rank k from ``eof_variance`` (default ~90% of variance).
# DECISION (D5.3): **seasonal (month-varying) VAR.** The cross-variable correlation structure is
# seasonal — e.g. the cold-calm temp↔wind coupling is strong in winter, weak in the shoulder seasons.
# That contemporaneous coupling is carried by the VAR *dynamics* (persistent synoptic patterns predict
# each other), NOT the one-step innovations (whose cross-variable covariance is ≈0). A single
# stationary VAR reproduces only the annual-average coupling and loses the winter cold-calm link (the
# step-(iv) demand–RES correlation "killer test" flagged this). We therefore fit a **per-month VAR**
# (coefficients + innovation covariance + score variances), sharing only the EOF basis; during a
# season the process converges to that month's stationary covariance, so the seasonal contemporaneous
# cross-variable correlation is reproduced.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Dependence:
    col_mean: np.ndarray        # (D,)
    col_std: np.ndarray         # (D,)
    components: np.ndarray      # (k, D) EOF loadings (orthonormal rows)
    resid_std: np.ndarray       # (D,) std of discarded-mode residual per latent column
    resid_phi: np.ndarray       # (D,) AR(1) coeff of the residual (preserves persistence/spells)
    resid_innov: np.ndarray     # (D,) AR(1) innovation std
    var_coef_m: np.ndarray      # (12, k*p, k) MONTHLY stacked VAR coefficients
    innov_chol_m: np.ndarray    # (12, k, k) Cholesky of the MONTHLY innovation covariance
    score_std_m: np.ndarray     # (12, k) monthly PC std (re-imposed per month to stop drift)
    score_cov_chol: np.ndarray  # (k, k) Cholesky of stationary PC covariance (seeding)
    order: int
    n_modes: int

    def simulate(self, months: np.ndarray, rng: np.random.Generator, burn: int = 300) -> np.ndarray:
        """Standard-normal latent field of shape (len(months), D); the VAR coefficients + innovation
        covariance track the calendar month of each step, so the seasonal cross-variable correlation is
        reproduced."""
        months = np.asarray(months, dtype=int)
        n_steps = len(months)
        k, p = self.n_modes, self.order
        total = n_steps + burn
        mon = np.concatenate([np.full(burn, months[0]), months]) - 1        # 0-based month per step
        Z = np.empty((total, k))
        Z[:p] = rng.standard_normal((p, k)) @ self.score_cov_chol.T
        white = rng.standard_normal((total, k))
        innov = np.empty((total, k))
        for m in range(12):
            sel = mon == m
            if sel.any():
                innov[sel] = white[sel] @ self.innov_chol_m[m].T
        hist = Z[:p][::-1].reshape(-1).copy()
        for t in range(p, total):
            zt = hist @ self.var_coef_m[mon[t]] + innov[t]
            Z[t] = zt
            hist = np.concatenate((zt, hist[:-k])) if p > 1 else zt
        Z = Z[burn:]
        # re-impose per-month PC variances (guards a near-unit-root VAR; per-column scale preserves the
        # seasonal cross-mode structure carried by the monthly dynamics)
        m0 = months - 1
        for m in range(12):
            sel = m0 == m
            if sel.sum() > 2:
                Z[sel] = Z[sel] * (self.score_std_m[m] / (Z[sel].std(axis=0) + 1e-9))
        Yc = Z @ self.components                                          # (n, D)
        n, D = Yc.shape
        e = rng.standard_normal((n, D)) * self.resid_innov
        r = np.empty((n, D))
        r[0] = rng.standard_normal(D) * self.resid_std
        for t in range(1, n):
            r[t] = self.resid_phi * r[t - 1] + e[t]
        Yc = Yc + r
        return Yc * self.col_std + self.col_mean


def _chol(cov: np.ndarray) -> np.ndarray:
    cov = np.atleast_2d(cov) + 1e-10 * np.eye(np.atleast_2d(cov).shape[0])
    try:
        return np.linalg.cholesky(cov)
    except np.linalg.LinAlgError:
        w, V = np.linalg.eigh(cov)
        return V @ np.diag(np.sqrt(np.clip(w, 0, None)))


def _var_design(Z: np.ndarray, p: int) -> tuple[np.ndarray, np.ndarray]:
    T = Z.shape[0]
    X = np.column_stack([Z[p - 1 - i: T - 1 - i] for i in range(p)])       # (T-p, k*p)
    return X, Z[p:]


def fit(gauss_field: np.ndarray, months: np.ndarray, eof_variance: float, var_order: int,
        copula: str, rng: np.random.Generator) -> Dependence:
    Y = np.where(np.isnan(gauss_field), 0.0, gauss_field)
    months = np.asarray(months, dtype=int)
    col_mean = Y.mean(axis=0); col_std = Y.std(axis=0); col_std[col_std < 1e-8] = 1.0
    Yc = (Y - col_mean) / col_std

    U, S, Vt = np.linalg.svd(Yc, full_matrices=False)
    cum = np.cumsum(S**2) / np.sum(S**2)
    k = int(np.clip(np.searchsorted(cum, eof_variance) + 1, 1, Vt.shape[0]))
    components = Vt[:k]
    Z = Yc @ components.T

    resid = Yc - Z @ components
    resid_std = resid.std(axis=0)
    r0, r1 = resid[:-1], resid[1:]
    denom = (r0 * r0).sum(axis=0)
    resid_phi = np.clip(np.where(denom > 0, (r0 * r1).sum(axis=0) / denom, 0.0), -0.99, 0.99)
    resid_innov = resid_std * np.sqrt(np.clip(1 - resid_phi**2, 1e-6, 1.0))

    p = max(1, var_order)
    X, Yv = _var_design(Z, p)
    row_month = months[p:]                                    # month of the target row t
    contig = months[p:] == months[:-p]                        # t and t-p in the same (contiguous) month
    # global fallback VAR (for thin months)
    gcoef, *_ = np.linalg.lstsq(X, Yv, rcond=None)
    gcov = np.cov(Yv - X @ gcoef, rowvar=False) if k > 1 else np.array([[np.var(Yv - X @ gcoef)]])

    var_coef_m = np.empty((12, k * p, k))
    innov_chol_m = np.empty((12, k, k))
    score_std_m = np.empty((12, k))
    for m in range(12):
        sel = (row_month == m + 1) & contig
        zmask = months == m + 1
        score_std_m[m] = Z[zmask].std(axis=0) if zmask.sum() > 2 else Z.std(axis=0)
        if sel.sum() > (k * p + 10):
            coef, *_ = np.linalg.lstsq(X[sel], Yv[sel], rcond=None)
            res = Yv[sel] - X[sel] @ coef
            cov = np.cov(res, rowvar=False) if k > 1 else np.array([[res.var()]])
        else:
            coef, cov = gcoef, gcov
        var_coef_m[m] = coef
        innov_chol_m[m] = _chol(np.atleast_2d(cov))

    return Dependence(
        col_mean=col_mean, col_std=col_std, components=components, resid_std=resid_std,
        resid_phi=resid_phi, resid_innov=resid_innov, var_coef_m=var_coef_m,
        innov_chol_m=innov_chol_m, score_std_m=score_std_m,
        score_cov_chol=_chol(np.cov(Z, rowvar=False)), order=p, n_modes=k,
    )
