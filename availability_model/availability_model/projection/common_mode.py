"""Phase 4 — common-mode (generic-fault) event simulator: the price-tail driver.

A Poisson process fires generic-fault episodes at the calibrated rate (~1-in-22 yr). When one fires it
reproduces the calibrated excess-unavailability pulse: a set of same-family reactors (sampled so the
palier mix matches the observed N4/P'4-dominated targeting) go offline for an extended, staggered
stretch, building to the calibrated peak excess (~0.26 of the fleet) and sustaining ~the plateau length
— i.e. the 2021-22 stress-corrosion pattern. Most 20-year draws see zero events; a few see one; the rare
draw with an event carries the upper price quantiles.

Deterministic given (seed, draw).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from powersim_core.rng import substream

from ..calibration.model import CalibratedAvailability
from ..config import Config

# affected-unit count relative to peak_excess*fleet. The plateau (~285 d) far exceeds the stagger
# spread, so affected units nearly fully overlap and peak ≈ n_affected/fleet → inflation ≈ 1.0
# reproduces the calibrated peak excess (verified: mean peak ≈ 0.26).
_INFLATE = 1.0


def simulate_common_mode(config: Config, model: CalibratedAvailability, registry: pd.DataFrame,
                         draw: int = 0) -> pd.DataFrame:
    proj = config.section("projection")
    y0, y1 = proj["horizon"]["start_year"], proj["horizon"]["end_year"]
    horizon_years = y1 - y0 + 1
    cm = model.common_mode
    rng = substream(config.seed, draw, "common_mode")     # F4: single RNG authority (SeedSequence)

    hstart = pd.Timestamp(year=y0, month=1, day=1, tz="UTC")
    span_days = (pd.Timestamp(year=y1, month=12, day=31, tz="UTC") - hstart).days
    nuc = registry[(registry["technology"] == "nuclear") & registry["closure_year"].isna()].copy()
    fleet = len(nuc)
    peak_excess = float(cm["peak_excess_unavail"])
    plateau = float(cm["plateau_days"])
    stagger_days = float(cm["stagger_weeks_mean"]) * 7
    target_prob = cm.get("target_prob", {})

    # per-unit selection weight: palier target probability spread over that palier's units (+ epsilon)
    pal_counts = nuc["palier"].value_counts().to_dict()
    w = nuc["palier"].map(lambda p: target_prob.get(str(p), 0.0) / pal_counts.get(p, 1)).to_numpy(float)
    w = w + 1e-6
    w = w / w.sum()

    n_events = rng.poisson(float(cm["event_freq_per_year"]) * horizon_years)
    rows = []
    for _ in range(n_events):
        ev_start = hstart + pd.Timedelta(days=float(rng.uniform(0, span_days)))
        n_aff = min(fleet, int(round(peak_excess * fleet * _INFLATE)))
        idx = rng.choice(fleet, size=n_aff, replace=False, p=w)
        for j in idx:
            u = nuc.iloc[int(j)]
            s = ev_start + pd.Timedelta(days=float(rng.uniform(0, 2 * stagger_days)))
            dur = float(np.clip(np.exp(rng.normal(np.log(plateau), 0.3)), 60, 700))
            rows.append({"unit_id": u["unit_id"], "name": u["name"], "technology": "nuclear",
                         "state": "common_mode", "start": s, "end": s + pd.Timedelta(days=dur),
                         "duration_days": round(dur, 1), "capacity_mw": float(u["capacity_mw"]),
                         "draw": int(draw)})
    cols = ["unit_id", "name", "technology", "state", "start", "end", "duration_days", "capacity_mw", "draw"]
    return (pd.DataFrame(rows, columns=cols).sort_values("start").reset_index(drop=True)
            if rows else pd.DataFrame(columns=cols))
