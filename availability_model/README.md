# availability_model (step v)

Unit-level stochastic **availability** of the French dispatchable fleet — the supply-side twin of the
demand (iii) and RES (iv) models. It produces, for each weather draw, coherent hourly time series of
**available capacity** per unit/technology, combining:

- **Planned outages** — nuclear refuelling/inspection cycles (ASR / VP / VD) placed with the observed
  spring–summer seasonality, plus thermal maintenance; heavy-tailed overruns.
- **Forced outages** — per-unit Poisson events with heavy-tailed durations, a fitted long-term trend
  and a **±10 % user correction on the trend slope**, plus an age-creep term.
- **Common-mode events** — Poisson generic-fault episodes hitting a nuclear *palier* (à la the
  2021–23 stress-corrosion crisis): correlated, staggered, extended outages across a sampled fraction
  of same-design reactors. This module drives the upper price quantiles.
- **Weather derating** — river-cooled thermal units lose capacity in hot / low-flow summers, driven by
  the **same weather draws** as demand and RES → heat-wave demand spikes coincide with thermal cuts.
- **Hydro inflows** — reservoir/RoR energy availability from the shared weather cube.
- **Interconnectors** — BE/DE/CH/IT/ES/GB as pseudo-units with NTC by direction + un/availability.

Downstream, step (vi) stacks demand − RES − imports against this availability to form prices, so the
**cross-correlations with steps (iii)/(iv) must survive** — everything is driven off the shared draws.

## Status

**Phase 0 (scaffold) complete.** Package skeleton, config, schemas, DB-grounded fleet registry,
assumptions workbook (template + validating loader), meta/versioning, CLI, smoke tests. Calibration,
scheduling, projection and validation land in Phases 1–7 (see `DECISIONS.md`).

## Why outages are *inferred*, not read

There is no REMIT/unavailability table in `pricemodeling.db` and the RTE unavailability API is broken
on the portal right now, so outage history is **inferred from per-unit production**
(`rte_generation_per_unit`): sustained near-zero output = outage, classified planned vs forced by
duration. Wiring the proper REMIT feed is tracked as a TODO (task #41). See `DECISIONS.md` (D1).

## Usage

```bash
cd availability_model
avail-model init-workbook          # write assumptions_avail.xlsx (pre-filled defaults)
# edit the workbook: ±10% forced correction, closures / lifetime extensions, new builds
avail-model calibrate              # Phase 2+ : infer outages, fit distributions
avail-model project                # Phase 6+ : simulate availability from workbook + weather draws
avail-model validate               # Phase 7  : validation suite
```

`config.yaml` holds paths, the calibration window, the 2021–23 baseline exclusion, projection horizon
(2027–2046) and draw count. The workbook (`§5`) holds every user-editable assumption; `fleet_registry`
is regenerated from the DB but user-editable for closures/new builds.

## Conventions (shared with steps ii–iv)

UTC timestamps · pydantic config · pandera schemas · Parquet outputs (partitioned by draw) ·
reproducibility hashes (git / config / workbook / weather-cube) · phase-by-phase build with sign-off.
