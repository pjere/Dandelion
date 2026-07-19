"""DM Phase 1 offline tests: calendar correctness + QC on a crafted dirty series.
(The DB loaders are validated against the real database in the calibration run.)"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from demand_model.config import load_config
from demand_model.features.calendar import build_calendar
from demand_model.io.qc import qc_demand

CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"


def test_calendar_holidays_ponts_dst():
    idx = pd.date_range("2024-01-01", "2024-12-31 23:00", freq="h", tz="UTC")
    cal = build_calendar(idx)
    # France has 11 public holidays -> ~11 days flagged (allow a couple for observed shifts)
    assert 10 * 24 <= int(cal.is_holiday.sum()) <= 13 * 24
    # New Year's Day (local) is a holiday
    ny = cal.loc[cal.index.tz_convert("Europe/Paris").normalize() == pd.Timestamp("2024-01-01", tz="Europe/Paris")]
    assert ny["is_holiday"].all()
    # DST: both winter (+1) and summer (+2) offsets appear
    assert set(np.round(cal["utc_offset_h"].unique())) == {1.0, 2.0}
    # summer school holidays flagged
    assert cal.loc[cal.index.month == 8, "school_frac"].mean() > 0.9
    # a pont exists (e.g. around 8/15 or 5/1 bridges) at least somewhere in the year
    assert cal["is_pont"].any()


def test_qc_flags_spike_and_gap():
    cfg = load_config(CONFIG)
    idx = pd.date_range("2019-01-01", periods=500, freq="h", tz="UTC")
    x = 50000 + 5000 * np.sin(np.arange(500) / 24)
    x[100] = x[99] + 40000            # isolated spike up
    x[101] = x[99]                    # ...back down
    df = pd.DataFrame({"timestamp_utc": idx, "load_mw": x})
    df = df.drop(index=[200, 201, 202]).reset_index(drop=True)   # 3-hour gap
    clean, rep = qc_demand(df, cfg)
    assert rep.n_hours == 500                      # reindexed to full grid
    assert rep.pct_missing > 0                      # the gap detected
    assert np.isnan(clean.set_index("timestamp_utc").loc[idx[100], "load_mw"])   # spike removed
    assert rep.n_spikes >= 1
