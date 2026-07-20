"""Linear-chain CRF over the marginal-tranche sequence — numpy + scipy only (no new dependencies).

**Why a CRF and not a plain classifier.** Which tranche is marginal at hour *t* is not independent of
*t−1*: ramp rates, minimum up/down times and start-up costs make some tranche→tranche transitions cheap
and others physically impossible. A per-hour classifier cannot express that; a linear-chain CRF puts it
directly in the **transition matrix**, which is learned from data and is inspectable afterwards — you can
read off which switches the market actually makes. This is the Markov structure the model needs.

Bidirectional context is legitimate here. The day-ahead auction clears the whole day **jointly**, so
conditioning hour *t* on hour *t+1* is not lookahead cheating — it is how the market actually works. This
is sequence *labelling*, not causal forecasting.

**Partial supervision is built into the objective, not bolted on.** The label is latent and derived
(see `labels.py`), so many hours are genuinely unlabelled or too ambiguous to trust. Rather than dropping
those rows — which would break the chain and destroy exactly the temporal structure we are trying to
learn — an unlabelled position is **marginalised over**: the numerator sums over all labels it could have
taken. The loss is

    -log P(observed labels) = logZ_free - logZ_clamped

where the clamped pass restricts labelled positions to their label and leaves the rest free. Dropping the
uncertain hours instead would both sever the sequences and quietly bias training toward easy hours.

Gradients come from forward-backward marginals (`dlogZ/dscore = posterior marginal`), so training is exact
L-BFGS on the true likelihood — no autodiff framework required.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize
from scipy.special import logsumexp

NEG = -1e9          # a "forbidden" score; -inf would produce NaN in the backward pass


def _forward_backward(scores: np.ndarray, trans: np.ndarray):
    """Exact marginals for a batch of equal-length chains.

    `scores` [B, L, K] emission log-potentials, `trans` [K, K] where `trans[j, k]` scores j→k.
    Returns `logZ` [B], unary posteriors [B, L, K], and pairwise posteriors summed over batch/time [K, K].
    """
    B, L, K = scores.shape
    alpha = np.empty((B, L, K))
    alpha[:, 0] = scores[:, 0]
    for t in range(1, L):
        alpha[:, t] = scores[:, t] + logsumexp(alpha[:, t - 1, :, None] + trans[None], axis=1)
    logZ = logsumexp(alpha[:, -1], axis=1)

    beta = np.zeros((B, L, K))
    for t in range(L - 2, -1, -1):
        beta[:, t] = logsumexp(trans[None] + (scores[:, t + 1] + beta[:, t + 1])[:, None, :], axis=2)

    unary = np.exp(alpha + beta - logZ[:, None, None])
    pair = np.zeros((K, K))
    for t in range(L - 1):
        lp = (alpha[:, t, :, None] + trans[None]
              + (scores[:, t + 1] + beta[:, t + 1])[:, None, :] - logZ[:, None, None])
        pair += np.exp(lp).sum(axis=0)
    return logZ, unary, pair


def viterbi(scores: np.ndarray, trans: np.ndarray) -> np.ndarray:
    """Most likely label sequence per chain — the MAP decode. Returns [B, L] int labels."""
    B, L, K = scores.shape
    delta = scores[:, 0].copy()
    back = np.zeros((B, L, K), dtype=np.int32)
    for t in range(1, L):
        m = delta[:, :, None] + trans[None]
        back[:, t] = np.argmax(m, axis=1)
        delta = scores[:, t] + np.max(m, axis=1)
    out = np.zeros((B, L), dtype=np.int32)
    out[:, -1] = np.argmax(delta, axis=1)
    for t in range(L - 2, -1, -1):
        out[:, t] = back[np.arange(B), t + 1, out[:, t + 1]]
    return out


def sequence_score(scores: np.ndarray, trans: np.ndarray, path: np.ndarray) -> np.ndarray:
    """Unnormalised log-score of a given path per chain — used for best-vs-second-best deferral margins."""
    B, L, _ = scores.shape
    b = np.arange(B)
    s = scores[b, 0, path[:, 0]].copy()
    for t in range(1, L):
        s += scores[b, t, path[:, t]] + trans[path[:, t - 1], path[:, t]]
    return s


@dataclass
class CRFParams:
    """Emission weights (+ optional hidden layer) and the transition matrix."""
    W: np.ndarray            # [F, K] or [H, K] when hidden
    b: np.ndarray            # [K]
    trans: np.ndarray        # [K, K]
    W1: np.ndarray | None = None    # [F, H] hidden layer
    b1: np.ndarray | None = None    # [H]

    @property
    def n_classes(self) -> int:
        return len(self.b)


def _emissions(X: np.ndarray, p: CRFParams):
    """Emission scores [B, L, K] and the hidden activation (for the backward pass)."""
    if p.W1 is None:
        return X @ p.W + p.b, None
    h = np.tanh(X @ p.W1 + p.b1)
    return h @ p.W + p.b, h


def _pack(p: CRFParams) -> np.ndarray:
    parts = [p.W.ravel(), p.b, p.trans.ravel()]
    if p.W1 is not None:
        parts += [p.W1.ravel(), p.b1]
    return np.concatenate(parts)


def _unpack(theta: np.ndarray, F: int, K: int, H: int | None) -> CRFParams:
    i = 0
    d = H or F
    W = theta[i:i + d * K].reshape(d, K); i += d * K
    b = theta[i:i + K]; i += K
    trans = theta[i:i + K * K].reshape(K, K); i += K * K
    if H is None:
        return CRFParams(W, b, trans)
    W1 = theta[i:i + F * H].reshape(F, H); i += F * H
    b1 = theta[i:i + H]
    return CRFParams(W, b, trans, W1, b1)


def _loss_grad(theta, X, Y, F, K, H, l2):
    """Negative partial-label log-likelihood and its exact gradient.

    `Y` [B, L] holds the label index, or -1 for a position to **marginalise over** rather than drop.
    """
    p = _unpack(theta, F, K, H)
    scores, h = _emissions(X, p)

    # clamped pass: labelled positions restricted to their observed label, others left free
    clamp = np.zeros_like(scores)
    known = Y >= 0
    if known.any():
        forbid = np.ones_like(scores, dtype=bool)
        bi, ti = np.nonzero(known)
        forbid[bi, ti, :] = True
        forbid[bi, ti, Y[known]] = False
        forbid[~known] = False
        clamp[forbid] = NEG

    logZ_free, marg_free, pair_free = _forward_backward(scores, p.trans)
    logZ_clamp, marg_clamp, pair_clamp = _forward_backward(scores + clamp, p.trans)

    nll = float((logZ_free - logZ_clamp).sum())
    d_scores = marg_free - marg_clamp                       # [B, L, K]
    d_trans = pair_free - pair_clamp

    if H is None:
        d_W = np.einsum("blf,blk->fk", X, d_scores)
        d_b = d_scores.sum(axis=(0, 1))
        g = np.concatenate([d_W.ravel(), d_b, d_trans.ravel()])
    else:
        d_W = np.einsum("blh,blk->hk", h, d_scores)
        d_b = d_scores.sum(axis=(0, 1))
        d_h = d_scores @ p.W.T * (1.0 - h ** 2)
        d_W1 = np.einsum("blf,blh->fh", X, d_h)
        d_b1 = d_h.sum(axis=(0, 1))
        g = np.concatenate([d_W.ravel(), d_b, d_trans.ravel(), d_W1.ravel(), d_b1])

    nll += l2 * float(theta @ theta)
    return nll, g + 2 * l2 * theta


@dataclass
class CRF:
    """Linear-chain CRF with linear or one-hidden-layer emissions, trained by L-BFGS."""
    n_classes: int
    hidden: int | None = None
    l2: float = 1e-3
    max_iter: int = 200
    params: CRFParams | None = None

    def fit(self, X: np.ndarray, Y: np.ndarray, seed: int = 0, verbose: bool = False) -> CRF:
        """`X` [B, L, F] features, `Y` [B, L] labels with **-1 meaning "unlabelled, marginalise"**."""
        B, L, F = X.shape
        K, H = self.n_classes, self.hidden
        rng = np.random.default_rng(seed)
        d = H or F
        init = [rng.normal(0, 0.01, d * K), np.zeros(K), np.zeros(K * K)]
        if H is not None:
            init += [rng.normal(0, 1 / np.sqrt(F), F * H), np.zeros(H)]
        theta0 = np.concatenate(init)
        res = minimize(_loss_grad, theta0, args=(X, Y, F, K, H, self.l2), jac=True, method="L-BFGS-B",
                       options={"maxiter": self.max_iter, "disp": verbose})
        self.params = _unpack(res.x, F, K, H)
        self.nll_ = float(res.fun)
        return self

    def scores(self, X: np.ndarray) -> np.ndarray:
        return _emissions(X, self.params)[0]

    def predict(self, X: np.ndarray) -> np.ndarray:
        """MAP label sequence [B, L]."""
        return viterbi(self.scores(X), self.params.trans)

    def marginals(self, X: np.ndarray) -> np.ndarray:
        """Posterior P(y_t = k) [B, L, K] — the confidence signal the deferral detector consumes."""
        return _forward_backward(self.scores(X), self.params.trans)[1]

    def predict_no_chain(self, X: np.ndarray) -> np.ndarray:
        """Argmax of the emissions with the transition matrix ignored — the **no-CRF baseline**, which
        isolates exactly what the Markov structure buys over an equivalent per-hour classifier."""
        return np.argmax(self.scores(X), axis=2)

    def to_dict(self) -> dict:
        """Plain arrays for `powersim_core.serialize` (npz + JSON) — never pickle (ADR-6)."""
        p = self.params
        d = {"W": p.W, "b": p.b, "trans": p.trans,
             "n_classes": self.n_classes, "hidden": self.hidden, "l2": self.l2}
        if p.W1 is not None:
            d |= {"W1": p.W1, "b1": p.b1}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> CRF:
        m = cls(int(d["n_classes"]), d.get("hidden"), float(d.get("l2", 1e-3)))
        m.params = CRFParams(np.asarray(d["W"]), np.asarray(d["b"]), np.asarray(d["trans"]),
                             np.asarray(d["W1"]) if "W1" in d else None,
                             np.asarray(d["b1"]) if "b1" in d else None)
        return m
