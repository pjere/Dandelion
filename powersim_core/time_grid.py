"""Canonical time grid (§6) — the ONLY place a model time index is constructed.

Leap-day policy (ADR, resolves finding F5): the **model grid is 8760 h/year with Feb-29 dropped**,
matching the frozen weather cube (`simulation.nc` = 20×8760 h). Real-calendar sources (RTE/ENTSO-E, incl.
2020/2024) drop Feb-29 when curated onto the model grid, so joins never misalign after a leap year.
Regenerating the cube to a 8784-on-leap-years grid is out of scope (§9) and would invalidate the golden
baseline; if ever done, flip `DROP_FEB29` and re-baseline. Raw extracts keep their native calendar.

Everything is tz-aware UTC, hourly, gap-free, unique, sorted. `assert_canonical_grid` is called on every
curated/output write.
"""
from __future__ import annotations

import pandas as pd

UTC = "UTC"
DROP_FEB29 = True                    # model-grid leap-day policy (see ADR above)


def hourly_index(start, end) -> pd.DatetimeIndex:
    """Contiguous hourly tz-aware UTC index over [start, end) — real-calendar (raw layer)."""
    start = pd.Timestamp(start, tz=UTC) if pd.Timestamp(start).tz is None else pd.Timestamp(start)
    end = pd.Timestamp(end, tz=UTC) if pd.Timestamp(end).tz is None else pd.Timestamp(end)
    return pd.date_range(start, end, freq="h", inclusive="left", tz=UTC)


def model_year_index(year: int) -> pd.DatetimeIndex:
    """The 8760 h model grid for `year` (Feb-29 dropped per policy)."""
    idx = hourly_index(f"{year}-01-01", f"{year + 1}-01-01")
    if DROP_FEB29:
        idx = idx[~((idx.month == 2) & (idx.day == 29))]
    return idx


def model_index(start_year: int, end_year: int) -> pd.DatetimeIndex:
    """The model grid over [start_year, end_year] inclusive — 8760 h per year."""
    return model_year_index(start_year).append(
        [model_year_index(y) for y in range(start_year + 1, end_year + 1)]) \
        if end_year > start_year else model_year_index(start_year)


def to_model_grid(df: pd.DataFrame, col: str = "timestamp_utc") -> pd.DataFrame:
    """Drop Feb-29 rows so a real-calendar frame lands on the model grid."""
    ts = pd.DatetimeIndex(df[col])
    return df[~((ts.month == 2) & (ts.day == 29))].reset_index(drop=True)


def assert_canonical_grid(obj, name: str = "dataset", *, expect_8760: bool = False) -> None:
    """Validate an index or a DataFrame's timestamp column is canonical. Raises on violation."""
    idx = obj if isinstance(obj, pd.DatetimeIndex) else pd.DatetimeIndex(obj["timestamp_utc"])
    if idx.tz is None or str(idx.tz) != "UTC":
        raise ValueError(f"{name}: timestamp not tz-aware UTC (got {idx.tz})")
    if not idx.is_monotonic_increasing:
        raise ValueError(f"{name}: timestamps not sorted ascending")
    if idx.has_duplicates:
        raise ValueError(f"{name}: duplicate timestamps")
    deltas = idx.to_series().diff().dropna().unique()
    allowed = {pd.Timedelta(hours=1)}
    if DROP_FEB29:
        allowed.add(pd.Timedelta(days=1, hours=1))         # the Feb 28→Mar 1 seam on the model grid
    if not set(deltas) <= allowed:
        raise ValueError(f"{name}: non-hourly gaps present ({set(deltas) - allowed})")
    if expect_8760 and len(idx) % 8760 != 0:
        raise ValueError(f"{name}: expected whole 8760-hour years, got {len(idx)} rows")
