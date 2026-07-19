"""Phase 1 — quality control for the historical demand series.

Reindex to a continuous hourly UTC grid (gap detection); flag spikes (robust z on first
differences) and flat-lines (stuck value); flag the COVID and sobriety anomaly windows (kept,
not deleted — calibration uses them as regressors so the trend isn't poisoned). DST-duplicate
timestamps are already collapsed by the hourly aggregation in the loader.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..config import Config


@dataclass
class QCReport:
    n_hours: int
    pct_missing: float
    n_spikes: int
    n_flatline: int
    anomaly_flagged: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)


def _spike_mask(x: np.ndarray, thr: float = 8.0) -> np.ndarray:
    d = np.diff(x, prepend=x[0])
    med = np.nanmedian(d); mad = np.nanmedian(np.abs(d - med)) or 1e-9
    z = 0.6745 * (d - med) / mad
    m = np.zeros(x.size, dtype=bool)
    big = np.abs(z) > thr
    m[1:-1] = big[1:-1] & big[2:] & (np.sign(d[1:-1]) != np.sign(d[2:]))
    return m


def _flatline_mask(x: np.ndarray, run: int = 6) -> np.ndarray:
    m = np.zeros(x.size, dtype=bool)
    same = np.r_[False, x[1:] == x[:-1]]
    i = 0
    while i < x.size:
        if same[i]:
            j = i
            while j < x.size and same[j]:
                j += 1
            if (j - i) >= run - 1:
                m[i - 1:j] = True
            i = j
        else:
            i += 1
    return m


def qc_demand(load: pd.DataFrame, config: Config) -> tuple[pd.DataFrame, QCReport]:
    s = load.set_index("timestamp_utc")["load_mw"].sort_index()
    grid = pd.date_range(s.index.min(), s.index.max(), freq="1h", tz="UTC")
    s = s.reindex(grid)
    pct_missing = float(s.isna().mean() * 100)

    x = s.to_numpy(dtype="float64")
    finite = np.where(np.isnan(x), np.nanmedian(x), x)
    spikes = _spike_mask(finite)
    flat = _flatline_mask(finite)
    x[spikes] = np.nan
    n_spikes, n_flat = int(spikes.sum()), int(flat.sum())

    out = pd.DataFrame({"timestamp_utc": grid, "load_mw": x})
    # flag anomaly windows (kept for regressors)
    flags = pd.DataFrame(index=grid)
    anomaly = {}
    for name, win in config.section("data").get("anomaly_windows", {}).items():
        m = (grid >= pd.Timestamp(win["start"], tz="UTC")) & (grid <= pd.Timestamp(win["end"], tz="UTC"))
        flags[f"is_{name}"] = m
        anomaly[name] = int(m.sum())
    out = out.join(flags.reset_index(drop=True).set_axis(out.index))

    rep = QCReport(
        n_hours=len(grid), pct_missing=round(pct_missing, 2), n_spikes=n_spikes, n_flatline=n_flat,
        anomaly_flagged=anomaly,
        notes=[f"spikes→NaN: {n_spikes}", f"flatline flagged: {n_flat}",
               "COVID/sobriety flagged (kept as regressors), not deleted"],
    )
    return out, rep
