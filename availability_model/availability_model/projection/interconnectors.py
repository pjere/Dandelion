"""Phase 5 — cross-border interconnector availability (per border × direction).

Each border carries an import and an export NTC (from the workbook `interconnectors` sheet). Available
transfer capacity is the NTC minus stochastic forced outages (Poisson, calibrated so downtime ≈
`forced_unavail`) and an annual planned-maintenance block (≈ `planned_unavail` of the year). Step (vi)
uses the available NTC to cap cross-border flows. Deterministic given (seed, draw).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from powersim_core.rng import substream

from ..config import Config

_FORCED_MEAN_DAYS = 3.0                                             # mean forced interconnector outage
_PLANNED_MEAN_DAYS = 10.0                                           # one planned maintenance block/year


def interconnector_availability(config: Config, interconnectors: pd.DataFrame, draw: int = 0) -> pd.DataFrame:
    """Daily available NTC per border/direction over the horizon → [border, direction, day, available_ntc_mw]."""
    proj = config.section("projection")
    y0, y1 = proj["horizon"]["start_year"], proj["horizon"]["end_year"]
    hstart = pd.Timestamp(year=y0, month=1, day=1, tz="UTC")
    hend = pd.Timestamp(year=y1, month=12, day=31, tz="UTC")
    days = pd.date_range(hstart, hend, freq="D", tz="UTC")
    nd = len(days)
    rng = substream(config.seed, draw, "interconnectors")   # F4: single RNG authority (SeedSequence)

    rows = []
    for r in interconnectors.itertuples(index=False):
        ntc = float(r.ntc_mw)
        pf = float(getattr(r, "planned_unavail", 0.03) or 0.0)
        ff = float(getattr(r, "forced_unavail", 0.02) or 0.0)
        avail = np.ones(nd)
        # forced outages: Poisson events, downtime fraction ≈ ff → rate = ff*365/mean_dur per year
        rate = ff * 365.0 / _FORCED_MEAN_DAYS
        for y in range(y0, y1 + 1):
            for _ in range(rng.poisson(rate)):
                s = int((pd.Timestamp(year=y, month=1, day=1, tz="UTC") - hstart).days
                        + rng.uniform(0, 365))
                dur = int(np.clip(rng.exponential(_FORCED_MEAN_DAYS), 1, 30))
                avail[max(0, s):min(nd, s + dur)] = 0.0
            # one planned maintenance block per year, length ≈ pf*365
            plen = int(round(pf * 365))
            if plen > 0:
                ps = int((pd.Timestamp(year=y, month=1, day=1, tz="UTC") - hstart).days
                         + rng.uniform(0, 365 - plen))
                avail[max(0, ps):min(nd, ps + plen)] = np.minimum(avail[max(0, ps):min(nd, ps + plen)], 0.0)
        rows.append(pd.DataFrame({"border": r.border, "direction": r.direction, "day": days,
                                  "available_ntc_mw": (ntc * avail).round(0)}))
    return pd.concat(rows, ignore_index=True)
