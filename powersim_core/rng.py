"""RNG management (§5) — the single pattern, resolving finding F4.

One authority: a master seed + `numpy.random.SeedSequence.spawn` gives each draw a *statistically
independent* child stream keyed deterministically by `draw_id`. This is collision-free across processes
(50+ parallel draws) — unlike the ad-hoc `default_rng(seed + draw*K + salt)` scattered today, and unlike
the global `np.random.seed` in weathergen (which silently shares state under multiprocessing).

    rng = draw_rng(master_seed, draw_id)          # per-draw generator, reproducible & independent
    sub = substream(master_seed, draw_id, "wind") # named sub-stream within a draw (e.g. per component)
"""
from __future__ import annotations

import hashlib

import numpy as np


def _label_to_int(label: str) -> int:
    return int.from_bytes(hashlib.sha256(label.encode()).digest()[:8], "big")


def draw_rng(master_seed: int, draw_id: int) -> np.random.Generator:
    """Independent Generator for `draw_id` under `master_seed` (SeedSequence.spawn keyed by draw)."""
    ss = np.random.SeedSequence(entropy=int(master_seed), spawn_key=(int(draw_id),))
    return np.random.default_rng(ss)


def substream(master_seed: int, draw_id: int, label: str) -> np.random.Generator:
    """Named independent sub-stream within a draw (e.g. per weather variable / per technology)."""
    ss = np.random.SeedSequence(entropy=int(master_seed), spawn_key=(int(draw_id), _label_to_int(label)))
    return np.random.default_rng(ss)


def spawn_draws(master_seed: int, n_draws: int) -> list[np.random.Generator]:
    """A list of `n_draws` independent generators (children of one SeedSequence)."""
    children = np.random.SeedSequence(int(master_seed)).spawn(n_draws)
    return [np.random.default_rng(c) for c in children]
