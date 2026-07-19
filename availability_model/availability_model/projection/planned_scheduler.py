"""Phase 3 — nuclear-grade planned-outage scheduler.

Generates, per nuclear unit over the projection horizon, a calendar of refuelling/inspection outages
(ASR / VP / VD) that reproduces the calibrated structure:
  - refuelling cadence  — one outage per palier `cycle_months` (start-to-start), units staggered so the
    fleet doesn't refuel in lock-step;
  - type sequence       — a visite décennale (VD) every ~10 years; between VDs, ASR vs VP sampled at the
    calibrated per-palier frequency;
  - duration            — drawn from the fitted lognormal per type (already includes real overruns);
  - seasonal placement  — start month sampled from the calibrated seasonality (EDF concentrates outages
    Apr–Sep), so winter scarcity is preserved;
  - concurrency cap     — a greedy deconfliction keeps simultaneous nuclear planned outages under the
    grid-security limit (`projection.nuclear_max_concurrent_planned`).

Deterministic given (seed, draw): the same weather draw always yields the same planned calendar.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from powersim_core.rng import substream

from ..calibration.model import CalibratedAvailability
from ..config import Config

_TYPES = ("ASR", "VP", "VD")


def _sample_month(rng, seasonality: dict) -> int:
    months = np.arange(1, 13)
    w = np.array([max(0.0, seasonality.get(int(m), 1.0)) for m in months], float)
    w = w / w.sum() if w.sum() > 0 else np.ones(12) / 12
    return int(rng.choice(months, p=w))


def _seasonal_snap(due: pd.Timestamp, rng, seasonality: dict) -> pd.Timestamp:
    """Move a due date to the nearest occurrence of a season-sampled month (± a random day)."""
    m = _sample_month(rng, seasonality)
    cands = [pd.Timestamp(year=y, month=m, day=15, tz="UTC") for y in (due.year - 1, due.year, due.year + 1)]
    best = min(cands, key=lambda c: abs((c - due).days))
    return best + pd.Timedelta(days=int(rng.integers(-10, 11)))


def _draw_duration(rng, tp: dict, band: dict, mult: float = 1.0) -> float:
    mu, sig = tp.get("lognorm_mu"), tp.get("lognorm_sigma")
    if mu is None or sig is None or not np.isfinite(mu):
        d = tp.get("mean_days") or 0.5 * (band["min"] + band["max"])
    else:
        d = float(np.exp(rng.normal(mu, sig)))
    # #81: `mult` (>1) lengthens planned outages to realise the extended-planned unavailability that REMIT
    # discloses as scheduled (previously mislabelled forced). The band cap widens with mult so décennales
    # aren't clipped back to the un-extended ceiling.
    return float(np.clip(d * mult, max(5.0, band["min"] * 0.6), band["max"] * 1.8 * max(1.0, mult)))


def schedule_planned(config: Config, model: CalibratedAvailability, registry: pd.DataFrame,
                     draw: int = 0, rng=None) -> pd.DataFrame:
    proj = config.section("projection")
    bands = config.section("calibration")["planned_types"]
    hstart = pd.Timestamp(year=proj["horizon"]["start_year"], month=1, day=1, tz="UTC")
    hend = pd.Timestamp(year=proj["horizon"]["end_year"], month=12, day=31, tz="UTC")
    vd_period = pd.Timedelta(days=365.25 * proj.get("vd_period_years", 10))
    rng = rng or substream(config.seed, draw, "planned")   # F4: single RNG authority (SeedSequence)

    nuc = registry[(registry["technology"] == "nuclear") & registry["closure_year"].isna()]
    rows = []
    for _, u in nuc.iterrows():
        p = model.planned.get(str(u["palier"]))
        if not p:
            continue
        cycle = pd.Timedelta(days=30.44 * p["cycle_months"])
        # ASR/VP relative frequency (VD is scheduled decennially, not sampled)
        n_asr = p["types"]["ASR"]["n"] or 1
        n_vp = p["types"]["VP"]["n"] or 1
        p_asr = n_asr / (n_asr + n_vp)
        # stagger: first outage within the first cycle; VD phase spread across the décennale window
        start = _seasonal_snap(hstart + pd.Timedelta(days=rng.uniform(0, cycle.days)), rng, p["seasonality"])
        last_vd = hstart - pd.Timedelta(days=rng.uniform(0, vd_period.days))
        while start < hend:
            if start - last_vd >= vd_period:
                otype = "VD"; last_vd = start
            else:
                otype = "ASR" if rng.random() < p_asr else "VP"
            dur = _draw_duration(rng, p["types"][otype], bands[otype], mult=p.get("duration_mult", 1.0))
            end = start + pd.Timedelta(days=dur)
            rows.append({"unit_id": u["unit_id"], "name": u["name"], "palier": u["palier"],
                         "outage_type": otype, "start": start, "end": end,
                         "duration_days": round(dur, 1), "capacity_mw": float(u["capacity_mw"]),
                         "draw": int(draw)})
            start = _seasonal_snap(start + cycle, rng, p["seasonality"])
    sched = pd.DataFrame(rows).sort_values("start").reset_index(drop=True)
    return _deconflict(sched, hstart, hend, int(proj.get("nuclear_max_concurrent_planned", 22)))


def _deconflict(sched: pd.DataFrame, hstart, hend, cap: int) -> pd.DataFrame:
    """Greedily push outage starts later (in 1-week steps) so concurrent planned outages stay ≤ cap."""
    if sched.empty:
        return sched
    n_days = (hend - hstart).days + 1
    load = np.zeros(n_days, dtype=np.int16)                        # units in planned outage per day
    out = []
    for r in sched.itertuples(index=False):
        s, e = r.start, r.end
        for _ in range(60):                                        # up to ~1 year of weekly shifts
            i0 = max(0, (s - hstart).days)
            i1 = min(n_days, (e - hstart).days)
            if i1 <= i0 or load[i0:i1].max(initial=0) < cap:
                break
            s += pd.Timedelta(days=7); e += pd.Timedelta(days=7)
        i0, i1 = max(0, (s - hstart).days), min(n_days, (e - hstart).days)
        if i1 > i0:
            load[i0:i1] += 1
        d = r._asdict()
        d["start"], d["end"] = s, e
        out.append(d)
    return pd.DataFrame(out).sort_values("start").reset_index(drop=True)


def planned_metrics(config: Config, sched: pd.DataFrame, registry: pd.DataFrame) -> dict:
    """Diagnostics: implied planned unavailability, concurrency, seasonal placement."""
    proj = config.section("projection")
    hstart = pd.Timestamp(year=proj["horizon"]["start_year"], month=1, day=1, tz="UTC")
    hend = pd.Timestamp(year=proj["horizon"]["end_year"], month=12, day=31, tz="UTC")
    n_days = (hend - hstart).days + 1
    nuc = registry[(registry["technology"] == "nuclear") & registry["closure_year"].isna()]
    load = np.zeros(n_days, dtype=np.int16)
    month_days = np.zeros(13)
    for r in sched.itertuples(index=False):
        i0, i1 = max(0, (r.start - hstart).days), min(n_days, (r.end - hstart).days)
        if i1 > i0:
            load[i0:i1] += 1
        for m, c in pd.Series(pd.date_range(r.start, r.end, freq="D")).dt.month.value_counts().items():
            month_days[m] += c
    unit_days = len(nuc) * n_days
    seas = ({int(m): round(month_days[m] / month_days[1:].mean(), 2) for m in range(1, 13)}
            if month_days[1:].sum() else {})
    return {"planned_unavailability": round(load.sum() / unit_days, 3),
            "mean_concurrent": round(load.mean(), 1), "max_concurrent": int(load.max()),
            "n_outages": int(len(sched)),
            "by_type": {k: int(v) for k, v in sched["outage_type"].value_counts().items()},
            "seasonality_check": seas}
