"""powersim_core — the shared library extracted from the four+ models (§5).

Single home for the cross-cutting code currently duplicated across weathergen/demand/res/availability/
dispatch: the domain glossary, the canonical hourly-UTC time grid (the ONLY place an index is built),
RNG management (SeedSequence.spawn, draw_id → deterministic child seed), unit conversions, and metadata
stamping. Lighter-touch (importable package, no monorepo) per the solo-project decision.
"""
from __future__ import annotations

from . import glossary, meta, rng, time_grid, units  # noqa: F401

__version__ = "0.1.0"
