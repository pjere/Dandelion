"""Orchestration entry points. Phases 3-7 fill in project/validate."""
from __future__ import annotations

import json

from .config import Config


def calibrate(config: Config):
    """Fit the availability parameter set from the inferred catalogue + DB, persist model + report.

    Layering (D2): this fits the parameters; the workbook keeps the user knobs (±10% forced correction,
    closures / new builds, scenario) that the projection engine layers on top — so we do NOT overwrite
    the workbook here.
    """
    import numpy as np

    from .calibration.common_mode import calibrate_common_mode
    from .calibration.derating import calibrate_derating
    from .calibration.forced import calibrate_forced
    from .calibration.inflows import calibrate_inflows
    from .calibration.model import CalibratedAvailability
    from .calibration.planned import calibrate_planned
    from .io.fleet import build_fleet_registry
    from .io.outages import availability_summary, infer_outage_events

    registry = build_fleet_registry(config)
    events = infer_outage_events(config, registry)
    summary = availability_summary(config, registry)
    obs_days = dict(zip(summary["technology"], summary["observed_unit_days"]))

    planned = calibrate_planned(events, registry, config)
    nuc_avail_ex = float(summary.loc[summary["technology"] == "nuclear", "availability_ex_crisis"].iloc[0])
    forced = calibrate_forced(events, obs_days, config, planned=planned, registry=registry,
                              baseline_unavail=1 - nuc_avail_ex)
    common = calibrate_common_mode(events, registry, config)
    derating = calibrate_derating(registry, config)
    inflows = calibrate_inflows(config)

    # Re-anchor nuclear forced to the MEASURED planned unavailability from the actual scheduler (a dry-run
    # on draw 0) so planned + forced reproduces the observed Kd self-consistently.
    from .projection.planned_scheduler import planned_metrics, schedule_planned
    _shim = CalibratedAvailability(planned=planned, forced=forced, common_mode=common,
                                   derating=derating, inflows=inflows)
    measured_planned = planned_metrics(config, schedule_planned(config, _shim, registry, draw=0),
                                       registry)["planned_unavailability"]
    # #81: split baseline unavailability by the REMIT ground-truth forced share (≈10%), not the duration
    # heuristic's ~40%. Forced becomes a small fixed budget; the rest is realised as EXTENDED PLANNED via a
    # scheduler duration multiplier — Kd preserved, split now matches REMIT.
    forced_share = config.section("calibration").get("forced_share_nuclear")
    forced = calibrate_forced(events, obs_days, config, planned=planned, registry=registry,
                              baseline_unavail=1 - nuc_avail_ex, planned_unavail_override=measured_planned,
                              forced_share=forced_share)
    mult = forced.get("nuclear", {}).get("planned_duration_mult")
    if mult:
        for p in planned.values():
            p["duration_mult"] = round(float(mult), 3)

    nuc = summary[summary["technology"] == "nuclear"].iloc[0]
    metrics = {
        "n_units": int(len(registry)), "n_nuclear": int((registry["technology"] == "nuclear").sum()),
        "n_events": int(len(events)),
        "nuclear_availability_all": float(nuc["availability_all"]),
        "nuclear_availability_ex_crisis": float(nuc["availability_ex_crisis"]),
        "availability_summary": summary.to_dict("records"),
    }
    model = CalibratedAvailability(planned=planned, forced=forced, common_mode=common,
                                   derating=derating, inflows=inflows, metrics=metrics)
    mpath = model.save(config.models_dir / "calibrated_availability.json")

    def _clean(o):                                                  # make report JSON-safe
        if isinstance(o, dict):
            return {k: _clean(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [_clean(v) for v in o]
        if isinstance(o, (np.floating, np.integer)):
            return o.item()
        if isinstance(o, float) and (np.isnan(o) or np.isinf(o)):
            return None
        return o

    rpath = config.reports_dir / "calibration_report.json"
    rpath.parent.mkdir(parents=True, exist_ok=True)
    inflows_lite = {k: (v if k != "reservoir" else {kk: vv for kk, vv in v.items()
                    if kk != "seasonal_profile_week"}) for k, v in inflows.items()}
    with rpath.open("w", encoding="utf-8") as fh:
        json.dump(_clean({"planned": planned, "forced": forced, "common_mode": common,
                          "derating": derating, "inflows": inflows_lite,
                          "metrics": {k: metrics[k] for k in
                          ("n_units", "n_nuclear", "n_events", "nuclear_availability_all",
                           "nuclear_availability_ex_crisis")}}), fh, indent=2)

    print(f"[calibrate] nuclear availability {nuc['availability_ex_crisis']:.3f} ex-crisis / "
          f"{nuc['availability_all']:.3f} all | {len(events)} events")
    print(f"[calibrate] forced(nuclear) freq={forced['nuclear']['freq_per_unit_year']}/unit-yr "
          f"(multi-day full; ref band {forced['nuclear']['literature_band_reference']})")
    print(f"[calibrate] common-mode: baseline_unavail={common['baseline_unavail']} "
          f"peak_excess={common['peak_excess_unavail']} -> crisis_avail~{common['implied_crisis_availability']} "
          f"| paliers {common['target_prob']}")
    print(f"[calibrate] model -> {mpath}")
    print(f"[calibrate] report -> {rpath}")
    return model


def project(config: Config, n_draws: int | None = None):
    from .projection.engine import project as _project
    return _project(config, n_draws=n_draws)


def run_validation(config: Config, n_draws: int = 15):
    from .validation.report import write_methodology
    from .validation.suite import run as run_suite

    res = run_suite(config, n_draws=n_draws)
    rdir = config.reports_dir
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "validation_report.json").write_text(json.dumps(res, indent=2, default=str))
    meth = write_methodology(config, res)

    icon = {"PASS": "PASS", "WARN": "warn", "FAIL": "FAIL"}
    print("[validate] availability_model §7 acceptance:")
    for c in res["checks"]:
        print(f"  [{icon[c['status']]}] {c['check']}: {c['detail']}")
    s = res["summary"]
    print(f"[validate] {s['pass']} PASS / {s['warn']} WARN / {s['fail']} FAIL "
          f"({res['metrics']['n_event_draws']}/{res['metrics']['n_draws']} draws had a common-mode event)")
    print(f"[validate] report -> {rdir / 'validation_report.json'} | methodology -> {meth}")
    return res
