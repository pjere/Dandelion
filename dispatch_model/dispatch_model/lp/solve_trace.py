"""Passive per-window solve-path recorder — metadata only, never a solve decision.

WHY THIS EXISTS. The warm-start / lazy-rows performance work is gated on A/B comparisons that must
distinguish a real behaviour change from wall-clock noise, and this model has a large, legitimate source
of the latter: seam attempts run under a hard 90 s bound (`highs_solver.solve_multizone_highs`), so
identical code can legitimately take the seam path in one run and the cold path in the next. The repo has
that measured — two runs of the same horizon agreed in only 3577 of 3579 hours. Comparing prices without
knowing which path each window took therefore cannot separate "the optimisation changed the answer" from
"this window timed out half a second earlier today", and the only ways out of that are to widen tolerances
(refused) or to remove the time limits (which would A/B a code path nobody runs).

So every A/B arm records, per window: which flex spec actually succeeded, whether the seam attempt was
tried and failed, how many §51 fixed-point solves ran, whether the IPM rescue fired, and whether the window
was dropped. Price comparisons are then made only over windows whose path matches on both arms, with the
mismatches listed and re-solved to classify them.

IT ALSO FIXES A REAL GAP, independent of the performance work. `rolling.backtest`'s window loop drops a
window with a bare `continue` — no print, no counter — so a backtest year can come up short and say
nothing, while `rolling.projection` has reported its dropped windows all along. The multi-year gate runs on
the backtest.

METADATA ONLY, AND THAT IS LOAD-BEARING. Every hook returns immediately when no recorder is active, and no
hook is consulted by any branch that decides how to solve. The flag-off path therefore stops being
byte-identical in the strict sense (it executes an `is None` test per hook) and becomes VALUE-identical,
which is exactly the carve-out the `row_tags` tagging takes: correctness is established by element-wise
equality against the frozen golden references (`rtol=0, atol=1e-9`), not by code-object identity.

Usage:

    from ..lp import solve_trace
    with solve_trace.record() as tr:
        run_backtest(cfg, 2024, flexibility=True, write_lake=False)
    tr.frame().to_parquet("path.parquet")
"""
from __future__ import annotations

import pandas as pd

#: the active recorder, or None. Module-global by design: the hooks sit deep in the call stack (inside the
#: §51 loop and the HiGHS wrapper) and threading a handle through every signature would change the
#: production call contracts this program is meant to leave alone.
_TRACE: "SolveTrace | None" = None


class SolveTrace:
    """Accumulates one record per window. Fields are filled by the hooks as the window progresses."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._cur: dict | None = None

    def _new(self, key) -> dict:
        return {"window": str(key), "spec_used": None, "seam_failed": False,
                "n_iter_51": 0, "ipm_used": False, "n_solves": 0,
                "nrow": 0, "ncol": 0, "solve_s": 0.0, "dropped": True}

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows) if self.rows else pd.DataFrame(
            columns=list(self._new("").keys()))


class record:
    """Context manager activating a recorder for the enclosed run. Nested use restores the outer one."""

    def __enter__(self) -> SolveTrace:
        global _TRACE
        self._prev = _TRACE
        self.trace = SolveTrace()
        _TRACE = self.trace
        return self.trace

    def __exit__(self, *exc) -> bool:
        global _TRACE
        _TRACE = self._prev
        return False


def active() -> bool:
    return _TRACE is not None


# ---- hooks. Each is a no-op when inactive; none influences any solve decision. --------------------

def window_begin(key) -> None:
    if _TRACE is None:
        return
    if _TRACE._cur is not None:                 # a window that never reached window_end (exception path)
        _TRACE.rows.append(_TRACE._cur)
    _TRACE._cur = _TRACE._new(key)


def spec_attempt(kind: str) -> None:
    """`kind` is "seam" or "cold" — the spec variant about to be tried."""
    if _TRACE is None or _TRACE._cur is None:
        return
    _TRACE._cur["spec_used"] = kind


def spec_failed(kind: str) -> None:
    if _TRACE is None or _TRACE._cur is None:
        return
    if kind == "seam":
        _TRACE._cur["seam_failed"] = True


def note_iterations(n: int) -> None:
    """§51 fixed-point solve count for the attempt that succeeded."""
    if _TRACE is None or _TRACE._cur is None:
        return
    _TRACE._cur["n_iter_51"] = int(n)


def note_solve(nrow: int, ncol: int, seconds: float) -> None:
    if _TRACE is None or _TRACE._cur is None:
        return
    c = _TRACE._cur
    c["n_solves"] += 1
    c["nrow"], c["ncol"] = int(nrow), int(ncol)      # last solve's dimensions (seam adds rows, never cols)
    c["solve_s"] += float(seconds)


def note_ipm() -> None:
    if _TRACE is None or _TRACE._cur is None:
        return
    _TRACE._cur["ipm_used"] = True


def window_end(ok: bool) -> None:
    if _TRACE is None or _TRACE._cur is None:
        return
    _TRACE._cur["dropped"] = not bool(ok)
    _TRACE.rows.append(_TRACE._cur)
    _TRACE._cur = None
