"""Phase 1 — calendar features. Timestamps are UTC; day-type/hour are derived in **Europe/Paris**
(DST 23/25-hour days handled by the tz conversion, not by hand).

Public holidays + ponts come from ``workalendar`` (France). School vacations are represented as a
national fraction (share of zones A/B/C on holiday) — appropriate for a *national* load model.

# NOTE: exact zone-A/B/C school dates need the official Éducation Nationale calendar; the windows
# here are the standard national periods (summer/Toussaint/Christmas exact; Feb/spring approximated
# as a partial-fraction window). Documented as a maintainable input, flagged in the report.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np
import pandas as pd

TZ = "Europe/Paris"


@lru_cache(maxsize=8)
def _holidays(y0: int, y1: int) -> frozenset:
    from workalendar.europe import France
    cal = France()
    days = set()
    for y in range(y0, y1 + 1):
        days.update(d for d, _ in cal.holidays(y))
    return frozenset(days)


def _school_fraction(dates: pd.DatetimeIndex) -> np.ndarray:
    """National share of school zones on vacation (0..1). Summer/Toussaint/Christmas = 1;
    Feb winter & April spring staggered zones ≈ 0.6; else 0."""
    doy = dates.dayofyear.to_numpy()
    md = dates.month * 100 + dates.day
    frac = np.zeros(len(dates))
    frac[(md >= 708) & (md <= 831)] = 1.0                 # summer
    frac[(md >= 1019) & (md <= 1103)] = 1.0               # Toussaint
    frac[(md >= 1221) | (md <= 103)] = 1.0                # Christmas
    frac[(doy >= 36) & (doy <= 74)] = np.maximum(frac[(doy >= 36) & (doy <= 74)], 0.6)   # Feb winter
    frac[(doy >= 100) & (doy <= 138)] = np.maximum(frac[(doy >= 100) & (doy <= 138)], 0.6)  # April spring
    return frac


def build_calendar(timestamp_utc: pd.DatetimeIndex) -> pd.DataFrame:
    """Return calendar features indexed by the (UTC) timestamps."""
    t = pd.DatetimeIndex(timestamp_utc)
    local = t.tz_convert(TZ)
    dates = local.normalize()
    hol = _holidays(int(local.year.min()), int(local.year.max()))
    is_hol = pd.Series(dates.date, index=t).isin(hol).to_numpy()
    dow = local.dayofweek.to_numpy()                      # 0=Mon..6=Sun

    # ponts: a working weekday adjacent (across a single weekday) to a holiday+weekend
    hol_dates = pd.DatetimeIndex(sorted(hol))
    is_pont = np.zeros(len(t), dtype=bool)
    if len(hol_dates):
        holset = {pd.Timestamp(d).date() for d in hol_dates}
        # Friday that is not holiday but Thursday is holiday -> pont ; Monday before Tuesday holiday
        thu_hol = pd.Series([(x - pd.Timedelta(days=1)) in holset for x in dates.date], index=t).to_numpy()
        tue_hol = pd.Series([(x + pd.Timedelta(days=1)) in holset for x in dates.date], index=t).to_numpy()
        is_pont = ((dow == 4) & thu_hol) | ((dow == 0) & tue_hol)
    is_pont = is_pont & ~is_hol

    day_type = np.where(is_hol, "holiday",
                np.where(is_pont, "pont",
                np.where(dow == 5, "sat",
                np.where(dow == 6, "sun",
                np.where(dow == 0, "mon",
                np.where(dow == 4, "fri", "tue_thu"))))))

    # DST: local day length != 24h detection via utc offset change
    offset_h = np.array([x.utcoffset().total_seconds() / 3600 for x in local])

    return pd.DataFrame({
        "timestamp_utc": t,
        "hour_local": local.hour.to_numpy(),
        "dow": dow,
        "month": local.month.to_numpy(),
        "day_type": day_type,
        "is_holiday": is_hol,
        "is_pont": is_pont,
        "school_frac": _school_fraction(local),
        "utc_offset_h": offset_h,
    }).set_index("timestamp_utc")
