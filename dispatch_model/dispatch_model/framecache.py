"""Bounded memo for the expensive DataFrame loaders, with copy-on-return.

WHY NOT `lru_cache`. The loaders take a `Config` (unhashable in practice) and a `zones` LIST, so they
cannot be decorated directly; and more importantly they return **mutable** DataFrames. Handing the same
frame to two callers is a latent corruption: the first to mutate it silently changes what the second
receives, and the failure would surface far from its cause — as a wrong price, not as an exception. Every
hit therefore returns a COPY, and the cached object is never exposed. That copy costs milliseconds against
loader rebuilds measured in seconds, so it is bought cheaply.

WHAT THIS IS FOR. `projection._preload` calls the same loaders repeatedly for overlapping (zone, year)
pairs, because `hydro.water_value.load_curves` is invoked ~26 times — once per shape-year, then three
times per zone inside `seasonal_level_deltas` (annual + summer + rest). Measured redundancy after the
SQL-filter fix:

    build_neighbour_stack   46 calls,  31 distinct  -> x1.5   123.4 s
    load_generation_hist    42 calls,  24 distinct  -> x1.8   122.9 s
    _observed_prices        25 calls,   4 distinct  -> x6.2    14.4 s

The keys are the data-determining inputs, NOT `id(config)`: a config object's identity says nothing about
which database it points at, and an id can be reused after garbage collection. Keys resolve the sqlite
path instead, so two Config instances over the same lake share cache entries — which is correct — and a
config pointed at a different lake cannot collide.

BOUNDED, and deliberately so, unlike `neighbours._measured_chp_mw`: those were small tuples, these are
frames of ~140k rows (~5 MB each). The bound is FIFO rather than LRU because the access pattern here is a
sweep over distinct keys, not reuse of a hot subset — and it was exactly an LRU under a sweep that
produced the 0 % hit rate this work started from.
"""
from __future__ import annotations

from collections import OrderedDict

import pandas as pd


def db_key(config) -> str:
    """The data-determining part of a Config: which lake it reads."""
    try:
        return str(config.resolve(config.section("data")["sqlite_path"]))
    except Exception:                                       # noqa: BLE001 — degrade to no sharing
        return repr(config)


class FrameCache:
    """FIFO-bounded {key: DataFrame} with copy-on-return. Not thread-safe by design (the model is
    single-threaded per process; `rolling.montecarlo` parallelises by PROCESS, so each worker gets its
    own instance and no frame crosses a process boundary)."""

    def __init__(self, maxsize: int = 48) -> None:
        self._d: OrderedDict = OrderedDict()
        self.maxsize = int(maxsize)
        self.hits = 0
        self.misses = 0

    def get_or_build(self, key, build):
        hit = self._d.get(key)
        if hit is not None:
            self.hits += 1
            return hit.copy()
        self.misses += 1
        val = build()
        if isinstance(val, pd.DataFrame):
            self._d[key] = val
            while len(self._d) > self.maxsize:
                self._d.popitem(last=False)                 # FIFO: drop the oldest inserted
            return val.copy()
        return val                                          # non-frame (empty/None): pass through, uncached

    def clear(self) -> None:
        self._d.clear()
        self.hits = self.misses = 0

    def info(self) -> str:
        n = self.hits + self.misses
        return (f"hits={self.hits} misses={self.misses} size={len(self._d)}/{self.maxsize} "
                f"hit_rate={self.hits / n * 100:.1f}%" if n else "unused")
