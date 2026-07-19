"""AVAIL Phase 1 — outage inference from per-unit production.

Uses the session-scoped cfg/registry/events fixtures (conftest.py); skips cleanly when the DB is absent.
"""
from __future__ import annotations

from availability_model.io.outages import availability_summary, daily_capacity_factor


def test_events_are_nuclear_only_and_well_formed(events):
    assert len(events) > 500                                        # a decade of a 56-reactor fleet
    assert set(events["technology"]) == {"nuclear"}                # merit-order peakers are NOT inferred
    assert (events["duration_days"] > 0).all()
    assert (events["end"] >= events["start"]).all()
    assert set(events["outage_type"]) <= {"forced", "ASR", "VP", "VD"}
    assert (events["capacity_mw"].between(800, 1800)).all()


def test_classification_is_duration_monotone(events):
    med = events.groupby("outage_type")["duration_days"].median()
    assert med["forced"] < med["ASR"] < med["VP"] < med["VD"]      # longer runs ⇒ heavier class


def test_nuclear_availability_matches_history(cfg, registry):
    summ = availability_summary(cfg, registry)
    nuc = summ[summ["technology"] == "nuclear"].iloc[0]
    assert nuc["inferable"]
    assert 0.70 <= nuc["availability_ex_crisis"] <= 0.80            # historical non-crisis Kd band
    assert nuc["availability_all"] < nuc["availability_ex_crisis"]  # 2021-23 crisis drags the mean down


def test_data_gaps_do_not_become_outages(cfg, registry):
    cf = daily_capacity_factor(cfg, registry)
    assert not cf.loc[cf["hours"] < 12, "is_outage"].any()         # sparse days can't be outages
