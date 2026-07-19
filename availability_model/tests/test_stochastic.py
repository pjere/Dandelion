"""AVAIL Phase 4 — forced-outage + common-mode stochastic processes. Session cfg/model/registry."""
from __future__ import annotations

import numpy as np
import pandas as pd
from availability_model.projection.common_mode import simulate_common_mode
from availability_model.projection.forced import simulate_forced
from availability_model.projection.planned_scheduler import schedule_planned


def _yrs(cfg):
    h = cfg.section("projection")["horizon"]
    return h["end_year"] - h["start_year"] + 1


def test_forced_rates_match_calibration(cfg, model, registry):
    f = simulate_forced(cfg, model, registry, draw=0)
    assert not f.empty and (f["duration_days"] > 0).all()
    n_nuc = ((registry["technology"] == "nuclear") & registry["closure_year"].isna()).sum()
    rate = (f["technology"] == "nuclear").sum() / (n_nuc * _yrs(cfg))
    assert 1.0 <= rate <= 1.7                                       # ~1.24 base + trend/age over horizon
    assert {"nuclear", "gas", "oil", "coal"} <= set(f["technology"])  # peakers included via literature EFOR


def test_forced_respects_planned(cfg, model, registry):
    sched = schedule_planned(cfg, model, registry, draw=0)
    n_free = (simulate_forced(cfg, model, registry, draw=0)["technology"] == "nuclear").sum()
    n_supp = (simulate_forced(cfg, model, registry, draw=0, planned=sched)["technology"] == "nuclear").sum()
    assert n_supp < n_free                                          # forced during a planned outage is dropped


def test_forced_reproducible(cfg, model, registry):
    a = simulate_forced(cfg, model, registry, draw=3)
    b = simulate_forced(cfg, model, registry, draw=3)
    assert a[["unit_id", "start"]].equals(b[["unit_id", "start"]])


def test_common_mode_frequency_and_peak(cfg, model, registry):
    y0 = cfg.section("projection")["horizon"]["start_year"]
    y1 = cfg.section("projection")["horizon"]["end_year"]
    hstart = pd.Timestamp(year=y0, month=1, day=1, tz="UTC")
    nd = (pd.Timestamp(year=y1, month=12, day=31, tz="UTC") - hstart).days + 1
    fleet = ((registry["technology"] == "nuclear") & registry["closure_year"].isna()).sum()

    n_with, peaks, pal_hits = 0, [], []
    N = 60
    for d in range(N):
        cm = simulate_common_mode(cfg, model, registry, draw=d)
        if cm.empty:
            continue
        n_with += 1
        load = np.zeros(nd)
        for r in cm.itertuples(index=False):
            i0, i1 = max(0, (r.start - hstart).days), min(nd, (r.end - hstart).days)
            if i1 > i0:
                load[i0:i1] += 1
        peaks.append(load.max() / fleet)
        pal = registry.set_index("unit_id")["palier"]
        pal_hits.extend(cm["unit_id"].map(pal).tolist())

    assert 0.4 <= n_with / N <= 0.85                               # Poisson(~0.9 events/draw) → ~0.59
    assert 0.22 <= np.mean(peaks) <= 0.32                          # reproduces calibrated peak excess 0.26
    hits = pd.Series(pal_hits).value_counts(normalize=True)
    assert hits.get("N4", 0) + hits.get("P'4", 0) > 0.5           # N4/P'4-dominated targeting (real)
