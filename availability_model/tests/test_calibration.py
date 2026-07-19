"""AVAIL Phase 2 — calibration. Uses the session-scoped `model` fixture (conftest.py)."""
from __future__ import annotations

from availability_model.calibration.model import CalibratedAvailability


def test_planned_paliers_are_sane(model):
    for palier, p in model.planned.items():
        assert 10 <= p["cycle_months"] <= 26, palier                # refuelling cadence, no startup artifacts
        for t in ("ASR", "VP", "VD"):                               # every type has a usable duration fit
            assert p["types"][t]["mean_days"] > 0 and p["types"][t]["lognorm_mu"] is not None
        assert abs(sum(p["seasonality"].values()) / 12 - 1.0) < 0.05  # weights normalised to mean 1
    # duration ordering ASR < VP < VD holds for the well-populated CPY palier
    cpy = model.planned["CPY"]["types"]
    assert cpy["ASR"]["mean_days"] < cpy["VP"]["mean_days"] < cpy["VD"]["mean_days"]


def test_epr_uses_fallback(model):
    epr = model.planned["EPR"]
    assert epr["reliable"] is False                                 # ~1 yr of Flamanville-3 data
    assert epr["cycle_months"] == 18.0                              # palier default, not the 3.9 startup artifact


def test_forced_sources(model):
    nuc = model.forced["nuclear"]
    # #81: forced is now a fixed REMIT share of the baseline (~10%), not the residual-into-forced anchoring
    assert nuc["source"] == "remit_share" and nuc["freq_per_unit_year"] > 0
    assert nuc["forced_gross_unavail"] > 0 and 0.20 <= nuc["baseline_unavail"] <= 0.32
    assert nuc["forced_share_used"] == 0.10                          # REMIT ground-truth split
    assert 1.2 <= nuc["planned_duration_mult"] <= 1.7               # rest realised as extended planned
    # forced is a small share of the baseline (REMIT ~0.08–0.13), not ~40%
    assert nuc["forced_gross_unavail"] / nuc["baseline_unavail"] <= 0.15
    assert model.forced["gas"]["source"] == "literature"


def test_common_mode_reproduces_crisis(model):
    cm = model.common_mode
    assert 0.40 <= cm["implied_crisis_availability"] <= 0.60        # ~observed 2022 low
    assert 1 / 30 <= cm["event_freq_per_year"] <= 1 / 15            # pinned to the return-period band
    tp = cm["target_prob"]
    assert tp.get("N4", 0) + tp.get("P'4", 0) > 0.5                 # crisis hit N4/P'4 hardest (real)


def test_derating_and_inflows(model):
    der = model.derating
    assert der["Channel"]["derate_frac_per_c"] == 0.0              # sea-cooled: no summer thermal derating
    assert der["Rhône"]["derate_frac_per_c"] > 0.0                 # river-cooled: sensitive
    assert model.inflows["reservoir"]["energy_capacity_gwh"] > 2000


def test_persistence_roundtrip(model, tmp_path):
    p = model.save(tmp_path / "m.pkl")                              # .pkl suffix normalised to .json
    assert p.suffix == ".json"                                     # F6: portable JSON, no pickle
    back = CalibratedAvailability.load(p)
    assert back.common_mode["peak_excess_unavail"] == model.common_mode["peak_excess_unavail"]
    # int keys must survive JSON (planned_scheduler indexes seasonality by int month)
    seas = back.planned["CPY"]["seasonality"]
    assert all(isinstance(k, int) for k in seas)
    assert seas == model.planned["CPY"]["seasonality"]
    assert back.inflows["reservoir"]["seasonal_profile_week"] == model.inflows["reservoir"]["seasonal_profile_week"]
