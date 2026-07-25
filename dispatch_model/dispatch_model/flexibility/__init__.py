"""Plant operating rigidities + downward bid ladder — endogenous negative-price formation (FLEX-F0…F8).

This package extends the dispatch LP with the operating rigidities of nuclear and fossil plants and a
correct downward bid ladder, so that negative prices emerge as balance duals. Everything here is **opt-in**
behind ``flexibility_module`` (see ``enabled``) and, when off, the LP is byte-identical to the pure model
(golden preserved). The module stays a **pure LP** — every rigidity is a continuous variable + linear
coupling constraint, so the hourly balance duals remain valid prices (no binaries anywhere).

Physics vs regime (spec §1.3): time-invariant physical parameters (α bands, ramps, xénon β, deep-mod caps)
are hard-coded per reactor class/vintage in ``reactor_class``; economically/regulatorily contingent
parameters (modulation cost, start cost, grid-stability floor, reserve requirements, OA ladder) are
year-indexed workbook trajectories in ``trajectories`` — never constants frozen over the horizon.
"""
from __future__ import annotations


def enabled(config) -> bool:
    """True if the flexibility module is switched on for this run (``flexibility.enabled`` in config.yaml).

    Default **False** → the dispatch LP is built exactly as before (byte-identical, golden preserved)."""
    try:
        return bool(config.section("flexibility").get("enabled", False))
    except (KeyError, AttributeError):
        return False
