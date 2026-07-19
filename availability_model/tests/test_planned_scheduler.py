"""AVAIL Phase 3 — planned-outage scheduler. Uses session cfg/registry/model fixtures."""
from __future__ import annotations

import numpy as np
from availability_model.projection.planned_scheduler import planned_metrics, schedule_planned


def test_schedule_structure_and_load(cfg, model, registry):
    sched = schedule_planned(cfg, model, registry, draw=0)
    assert len(sched) > 500
    assert (sched["end"] > sched["start"]).all()
    assert set(sched["outage_type"]) == {"ASR", "VP", "VD"}
    m = planned_metrics(cfg, sched, registry)
    # #81: planned now absorbs the extended scheduled maintenance REMIT discloses as planned (duration_mult),
    # so planned unavailability is the bulk of the ~0.26 baseline (forced is only ~10%), not the old ~0.17
    assert 0.18 <= m["planned_unavailability"] <= 0.28
    assert m["max_concurrent"] <= cfg.section("projection")["nuclear_max_concurrent_planned"]


def test_seasonality_is_summer_heavy(cfg, model, registry):
    m = planned_metrics(cfg, schedule_planned(cfg, model, registry, draw=0), registry)
    s = m["seasonality_check"]
    summer = np.mean([s[x] for x in (5, 6, 7, 8, 9)])
    winter = np.mean([s[x] for x in (12, 1, 2)])
    assert summer > 1.1 > winter                                  # EDF concentrates outages Apr–Sep


def test_vd_cadence(cfg, model, registry):
    sched = schedule_planned(cfg, model, registry, draw=0)
    years = cfg.section("projection")["horizon"]["end_year"] - cfg.section("projection")["horizon"]["start_year"] + 1
    n_units = ((registry["technology"] == "nuclear") & registry["closure_year"].isna()).sum()
    vd_per_unit = (sched["outage_type"] == "VD").sum() / n_units
    assert 0.7 * years / 10 <= vd_per_unit <= 1.6 * years / 10     # ~one décennale per 10 years


def test_reproducible_and_draw_varies(cfg, model, registry):
    a = schedule_planned(cfg, model, registry, draw=0)
    b = schedule_planned(cfg, model, registry, draw=0)
    c = schedule_planned(cfg, model, registry, draw=1)
    assert a[["unit_id", "start", "outage_type"]].equals(b[["unit_id", "start", "outage_type"]])  # deterministic
    assert not a["start"].reset_index(drop=True).equals(c["start"].reset_index(drop=True))         # draw varies
