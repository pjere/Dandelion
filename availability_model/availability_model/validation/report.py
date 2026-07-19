"""Phase 7 — methodology note generator (markdown), stamped with the live calibration + validation numbers."""
from __future__ import annotations

from pathlib import Path

from ..calibration.model import CalibratedAvailability
from ..config import Config


def write_methodology(config: Config, validation: dict) -> Path:
    m = CalibratedAvailability.load(config.models_dir / "calibrated_availability.json")
    nf = m.forced["nuclear"]
    cm = m.common_mode
    checks = "\n".join(f"| {c['check']} | {c['status']} | {c['detail']} |" for c in validation["checks"])
    met = validation["metrics"]

    md = f"""# availability_model (step v) — methodology note

Unit-level stochastic availability of the French dispatchable fleet, feeding the price step (vi). It
produces, per weather draw, coherent hourly available-capacity series that stay correlated with demand
(iii) and RES (iv) because all three consume the **same** weather cube.

## Fleet
DB-grounded registry: {m.metrics['n_nuclear']} nuclear units (paliers CP0/CPY/P4/P'4/N4/EPR) + peakers,
hydro, interconnectors. Capacities from the p99.9 of per-unit production (robust to data spikes).

## Historical outages — inferred, nuclear only
No REMIT table exists (API broken), so the outage catalogue is inferred from per-unit production for the
must-run nuclear fleet ({m.metrics['n_events']} events). Merit-order peakers idle economically, so their
rates come from literature EFOR, not inference. Technical availability (Kd) ex-crisis
**{m.metrics['nuclear_availability_ex_crisis']:.3f}** / all {m.metrics['nuclear_availability_all']:.3f}.

## Calibration
- **Planned** (ASR/VP/VD): per-palier lognormal durations, refuelling cycle, summer-heavy seasonality.
- **Forced**: nuclear residual-anchored — mean duration {nf.get('mean_duration_days', 'n/a')} d set so
  planned + forced reproduces the observed baseline unavailability ({nf.get('baseline_unavail','?')}); the
  short-forced fit alone misses extended unplanned outages. Peakers on literature EFOR.
- **Common-mode** (the price-tail driver): calibrated to the *excess* unavailability pulse of the 2021–23
  crisis — baseline {cm['baseline_unavail']}, peak excess {cm['peak_excess_unavail']} → crisis trough
  ~{cm['implied_crisis_availability']} (the real winter-2022 low). Palier targeting recovered from data:
  N4 {cm['target_prob'].get('N4','?')}, P'4 {cm['target_prob'].get("P'4",'?')} (the hardest-hit families).
  Frequency pinned to a {cm['return_years_target']}-yr return period.
- **Weather derating**: river/estuary units lose output on hot days (shared temperature draw).
- **Reservoir**: energy budget from RTE water reserves (usable {m.inflows['reservoir']['usable_energy_gwh']} GWh).

## Projection
Per draw: planned ∪ forced ∪ common-mode (union) zero a unit's capacity; derating scales it on hot days;
scenario knobs (closures / new builds, ±10 % forced correction) from the workbook. Weather is a single
shared 20-yr realization, so the demand↔availability coupling (heat wave → demand↑ ∧ thermal↓) holds.
Outputs: availability by technology, per-nuclear-unit detail, interconnector NTC, reservoir budget.

## Validation (§7)

| check | status | detail |
|---|---|---|
{checks}

Non-crisis Kd **{met['noncrisis_Kd']:.3f}**, common-mode return {met['return_years']:.0f} yr,
{met['n_event_draws']}/{met['n_draws']} validation draws carried a common-mode event.

## Known limitations
- Outage history is production-inferred, not REMIT ground truth (TODO: wire the unavailability feed).
- Weather derating uses national temperature + literature slopes (a river-temperature refit is a hook).
- Extended non-crisis outages are folded into the residual-anchored forced component rather than modelled
  as a separate prolongation process.
"""
    path = config.reports_dir / "methodology.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(md, encoding="utf-8")
    return path
