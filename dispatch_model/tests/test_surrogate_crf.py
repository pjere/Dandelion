"""CRF correctness against brute-force enumeration and finite-difference gradients.

A subtly wrong forward-backward still trains and still produces plausible-looking output, so these check
the maths directly on chains small enough to enumerate exhaustively.
"""
from __future__ import annotations

import itertools

import numpy as np
from dispatch_model.surrogate.crf import (
    CRF,
    _forward_backward,
    _loss_grad,
    sequence_score,
    viterbi,
)
from scipy.special import logsumexp


def _brute_paths(L, K):
    return list(itertools.product(range(K), repeat=L))


def _path_logscore(scores, trans, path):
    s = scores[0, path[0]]
    for t in range(1, len(path)):
        s += scores[t, path[t]] + trans[path[t - 1], path[t]]
    return s


def test_logZ_matches_brute_force_enumeration():
    rng = np.random.default_rng(0)
    L, K = 5, 3
    scores, trans = rng.normal(size=(1, L, K)), rng.normal(size=(K, K))
    logZ, _, _ = _forward_backward(scores, trans)
    want = logsumexp([_path_logscore(scores[0], trans, p) for p in _brute_paths(L, K)])
    assert np.isclose(logZ[0], want)


def test_unary_marginals_match_brute_force():
    rng = np.random.default_rng(1)
    L, K = 4, 3
    scores, trans = rng.normal(size=(1, L, K)), rng.normal(size=(K, K))
    _, unary, _ = _forward_backward(scores, trans)
    paths = _brute_paths(L, K)
    lp = np.array([_path_logscore(scores[0], trans, p) for p in paths])
    prob = np.exp(lp - logsumexp(lp))
    for t in range(L):
        for k in range(K):
            want = sum(prob[i] for i, p in enumerate(paths) if p[t] == k)
            assert np.isclose(unary[0, t, k], want)


def test_pairwise_marginals_sum_to_chain_length():
    rng = np.random.default_rng(2)
    B, L, K = 3, 6, 4
    scores, trans = rng.normal(size=(B, L, K)), rng.normal(size=(K, K))
    _, _, pair = _forward_backward(scores, trans)
    assert np.isclose(pair.sum(), B * (L - 1))      # one transition per adjacent pair, per chain


def test_viterbi_matches_brute_force():
    rng = np.random.default_rng(3)
    L, K = 6, 3
    scores, trans = rng.normal(size=(1, L, K)), rng.normal(size=(K, K))
    got = viterbi(scores, trans)[0]
    want = max(_brute_paths(L, K), key=lambda p: _path_logscore(scores[0], trans, p))
    assert tuple(got) == want


def test_sequence_score_consistent_with_bruteforce():
    rng = np.random.default_rng(4)
    L, K = 5, 3
    scores, trans = rng.normal(size=(1, L, K)), rng.normal(size=(K, K))
    path = viterbi(scores, trans)
    assert np.isclose(sequence_score(scores, trans, path)[0],
                      _path_logscore(scores[0], trans, path[0]))


def _fd_check(hidden):
    rng = np.random.default_rng(5)
    B, L, F, K = 2, 4, 3, 3
    X = rng.normal(size=(B, L, F))
    Y = rng.integers(0, K, size=(B, L))
    Y[0, 1] = -1                                    # an unlabelled position must be differentiable too
    H = hidden
    n = (H or F) * K + K + K * K + (F * H + H if H else 0)
    theta = rng.normal(0, 0.1, n)
    f0, g = _loss_grad(theta, X, Y, F, K, H, 1e-3)
    num = np.zeros_like(theta)
    eps = 1e-6
    for i in range(len(theta)):
        tp, tm = theta.copy(), theta.copy()
        tp[i] += eps; tm[i] -= eps
        num[i] = (_loss_grad(tp, X, Y, F, K, H, 1e-3)[0] - _loss_grad(tm, X, Y, F, K, H, 1e-3)[0]) / (2 * eps)
    assert np.allclose(g, num, atol=1e-4), np.abs(g - num).max()


def test_gradient_matches_finite_differences_linear():
    _fd_check(None)


def test_gradient_matches_finite_differences_mlp():
    _fd_check(4)


def test_loss_is_nonnegative_and_zero_when_certain():
    """logZ_free >= logZ_clamped always; fully unlabelled chains carry no information, so loss == 0."""
    rng = np.random.default_rng(6)
    B, L, F, K = 2, 5, 3, 3
    X = rng.normal(size=(B, L, F))
    theta = rng.normal(0, 0.1, F * K + K + K * K)
    all_unlabelled = -np.ones((B, L), dtype=int)
    nll, _ = _loss_grad(theta, X, all_unlabelled, F, K, None, 0.0)
    assert np.isclose(nll, 0.0, atol=1e-6)
    labelled = rng.integers(0, K, size=(B, L))
    nll2, _ = _loss_grad(theta, X, labelled, F, K, None, 0.0)
    assert nll2 > 0


def test_crf_learns_a_transition_structure():
    """A sequence whose label simply persists should be learnable, and the CRF should beat the
    transition-free baseline on it — that is the whole point of the chain."""
    rng = np.random.default_rng(7)
    B, L, F, K = 60, 20, 2, 2
    Y = np.zeros((B, L), dtype=int)
    for b in range(B):
        y = rng.integers(0, K)
        for t in range(L):
            if rng.random() < 0.05:
                y = 1 - y                            # rarely switches: strong self-transition
            Y[b, t] = y
    # features are only weakly informative, so the chain must supply the rest
    X = rng.normal(size=(B, L, F)) + 0.8 * np.eye(K)[Y][:, :, :F]
    m = CRF(K, l2=1e-3, max_iter=200).fit(X, Y, seed=0)
    acc = (m.predict(X) == Y).mean()
    acc_nochain = (m.predict_no_chain(X) == Y).mean()
    # The absolute level is a property of the synthetic signal-to-noise, not of the CRF; what must hold
    # is that the chain *earns its place* over an equivalent transition-free classifier, and that it
    # recovers the persistence actually present in the data.
    assert acc > acc_nochain + 0.05
    assert acc > 0.8
    assert m.params.trans[0, 0] > m.params.trans[0, 1]   # learned "stay" beats "switch"
    assert m.params.trans[1, 1] > m.params.trans[1, 0]


def test_roundtrip_serialisation_preserves_predictions():
    rng = np.random.default_rng(8)
    B, L, F, K = 10, 6, 3, 3
    X = rng.normal(size=(B, L, F))
    Y = rng.integers(0, K, size=(B, L))
    m = CRF(K, hidden=4, max_iter=20).fit(X, Y, seed=1)
    m2 = CRF.from_dict(m.to_dict())
    assert np.array_equal(m.predict(X), m2.predict(X))
