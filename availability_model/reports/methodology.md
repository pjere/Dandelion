# availability_model (step v) — methodology note

Unit-level stochastic availability of the French dispatchable fleet, feeding the price step (vi). It
produces, per weather draw, coherent hourly available-capacity series that stay correlated with demand
(iii) and RES (iv) because all three consume the **same** weather cube.

## Fleet
DB-grounded registry: 59 nuclear units (paliers CP0/CPY/P4/P'4/N4/EPR) + peakers,
hydro, interconnectors. Capacities from the p99.9 of per-unit production (robust to data spikes).

## Historical outages — inferred, nuclear only
No REMIT table exists (API broken), so the outage catalogue is inferred from per-unit production for the
must-run nuclear fleet (1633 events). Merit-order peakers idle economically, so their
rates come from literature EFOR, not inference. Technical availability (Kd) ex-crisis
**0.738** / all 0.722.

## Calibration
- **Planned** (ASR/VP/VD): per-palier lognormal durations, refuelling cycle, summer-heavy seasonality.
- **Forced**: nuclear residual-anchored — mean duration 7.7 d set so
  planned + forced reproduces the observed baseline unavailability (0.262); the
  short-forced fit alone misses extended unplanned outages. Peakers on literature EFOR.
- **Common-mode** (the price-tail driver): calibrated to the *excess* unavailability pulse of the 2021–23
  crisis — baseline 0.276, peak excess 0.26 → crisis trough
  ~0.464 (the real winter-2022 low). Palier targeting recovered from data:
  N4 0.637, P'4 0.252 (the hardest-hit families).
  Frequency pinned to a [15, 30]-yr return period.
- **Weather derating**: river/estuary units lose output on hot days (shared temperature draw).
- **Reservoir**: energy budget from RTE water reserves (usable 2593.0 GWh).

## Projection
Per draw: planned ∪ forced ∪ common-mode (union) zero a unit's capacity; derating scales it on hot days;
scenario knobs (closures / new builds, ±10 % forced correction) from the workbook. Weather is a single
shared 20-yr realization, so the demand↔availability coupling (heat wave → demand↑ ∧ thermal↓) holds.
Outputs: availability by technology, per-nuclear-unit detail, interconnector NTC, reservoir budget.

## Validation (§7)

| check | status | detail |
|---|---|---|
| noncrisis_nuclear_Kd | PASS | 0.765 vs [0.73, 0.78] |
| common_mode_crisis_trough | PASS | worst annual Kd 0.564 vs ~0.54 target (2022) |
| quiet_draws_no_false_crisis | PASS | worst annual Kd on quiet draws 0.670 (should stay > ~0.66) |
| common_mode_return_period | PASS | 22.5 yr vs [15, 30] |
| planned_summer_seasonality | PASS | summer 1.21 vs winter 0.68; planned unavail 0.211 |
| availability_bounded | PASS | mean Kd across draws in [0.749, 0.767] |
| weather_derating_coupling | PASS | 38 derated unit-days, summer-concentrated=True |

Non-crisis Kd **0.765**, common-mode return 23 yr,
5/8 validation draws carried a common-mode event.

## Known limitations
- Outage history is production-inferred, not REMIT ground truth (TODO: wire the unavailability feed).
- Weather derating uses national temperature + literature slopes (a river-temperature refit is a hook).
- Extended non-crisis outages are folded into the residual-anchored forced component rather than modelled
  as a separate prolongation process.
