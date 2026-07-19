"""Phase 1 — capacity-factor quality control (§3.A).

Flags (never deletes) periods that must not be fit on:
  * CF out of the physical range [0, cf_max] (normalisation/reporting glitches);
  * commissioning ramp-up (first weeks after a technology's capacity starts growing from ~0, or after
    first non-trivial production) — offshore especially, where partial-fleet months depress the CF;
  * flat-line runs (identical value held for many hours) — outages or grid curtailment.
Returns the CF_HIST contract with an ``is_valid`` mask.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import Config
from .schemas import CF_HIST, validate


def _flatline_mask(x: pd.Series, min_run: int) -> np.ndarray:
    """True where a value is held identical for >= min_run consecutive hours (and not ~0)."""
    v = x.to_numpy()
    same = np.r_[False, np.isclose(np.diff(v), 0.0)]
    run = np.zeros(len(v), dtype=int)
    c = 0
    for i in range(len(v)):
        c = c + 1 if same[i] else 0
        run[i] = c
    # back-fill the run length across the whole flat segment
    flat = run >= min_run
    for i in range(len(v) - 2, -1, -1):
        if flat[i + 1] and same[i + 1]:
            flat[i] = True
    return flat & (np.abs(v) > 1e-6)


def qc_capacity_factor(config: Config, cf: pd.DataFrame) -> pd.DataFrame:
    """Apply CF-range, ramp-up and flat-line flags per technology → CF_HIST."""
    q = config.section("data")["qc"]
    cf_max = float(q["cf_max"])
    ramp_days = int(q["ramp_up_days"])
    flat_h = int(q["flatline_hours"])

    out = []
    for _tech, g in cf.groupby("technology"):
        g = g.sort_values("timestamp_utc").copy()
        valid = g["cf"].notna() & g["production_mw"].notna()
        valid &= g["cf"].between(0.0, cf_max)

        # commissioning ramp-up: exclude the first ``ramp_days`` after production first exceeds 1% of
        # the eventual max (captures offshore fleets coming online gradually)
        prod = g["production_mw"].fillna(0.0).to_numpy()
        thr = 0.01 * np.nanmax(prod) if np.nanmax(prod) > 0 else 0.0
        onset = np.argmax(prod > thr) if (prod > thr).any() else 0
        onset_ts = g["timestamp_utc"].iloc[onset]
        valid &= g["timestamp_utc"] >= (onset_ts + pd.Timedelta(days=ramp_days))

        # flat-line runs (outage/curtailment)
        valid &= ~pd.Series(_flatline_mask(g["cf"].fillna(-1), flat_h), index=g.index)

        g["is_valid"] = valid.to_numpy()
        g["cf"] = g["cf"].clip(0.0, cf_max)
        out.append(g[["timestamp_utc", "technology", "region", "cf", "is_valid"]])
    res = pd.concat(out, ignore_index=True)
    return validate(res, CF_HIST, "cf")


def qc_report(cf_qc: pd.DataFrame) -> pd.DataFrame:
    """Per-technology summary: n, valid fraction, mean CF on the valid subset."""
    rows = []
    for tech, g in cf_qc.groupby("technology"):
        v = g[g["is_valid"]]
        rows.append({"technology": tech, "n": len(g),
                     "valid_pct": round(100 * g["is_valid"].mean(), 1),
                     "mean_cf_valid_pct": round(100 * v["cf"].mean(), 2) if len(v) else np.nan,
                     "start": g["timestamp_utc"].min(), "end": g["timestamp_utc"].max()})
    return pd.DataFrame(rows)
