# DECISIONS — availability_model (step v)

Unit-level stochastic availability of the FR dispatchable fleet: planned (maintenance/refuelling)
unavailability, forced outages with a long-term trend + ±10 % user correction, common-mode
(generic-fault) events across nuclear paliers, weather-linked deratings (river-cooled thermal), and
stochastic hydro inflows — all consuming the SAME weather draws as steps (iii)/(iv). Feeds step (vi).

## Blocking decisions (§10) — fixed with user before coding (2026-07-10)

| # | Question | Choice | Rationale |
|---|----------|--------|-----------|
| D1 | Outage history source | **INFER from per-unit production** (`rte_generation_per_unit`) | There is **no REMIT/outage table** in the DB — the RTE unavailability API (v7) was disabled at extraction and is **broken on the RTE site now**. Production is a rich proxy: verified on BELLEVILLE 1 (p99=1311 MW = its P4 capacity, 22.7 % outage-days ≈ 77 % Kd, 15 spells ≥3 d, longest 235 d = a décennale). Wiring the proper REMIT feed is a **TODO** (task #41). Calibration window 2015–2026 (per-unit coverage). **→ FEED BUILT (2026-07-18, task #41):** `pricemodeling/entsoe/unavailability.py` ingests REMIT to `entsoe_unavailability` (idempotent, CLI `ingest-remit`) and `reconstruct_daily_availability` collapses the overlapping messages into per-unit daily available MW; `outage_rate_summary` yields the ground-truth Kd + planned/forced split. Validated on **live** ENTSO-E data (FR Q1-2023: 1590 records, 124 units, planned/forced = 0.73/0.27). entsoe-py 0.8 returns human-readable businessType labels, not A53/A54 codes — parser handles both. **→ #78 (2026-07-18): backfilled FR REMIT 2015–2024, validated the inference, fed step-vi.** REMIT nuclear ground truth vs the production-inferred model: **availability level VALIDATED** — REMIT normal-year availability 0.66–0.78 (mean ~0.72) brackets the inferred **0.738 ex-crisis**, and REMIT 2022-crisis **0.50** matches the model's common-mode **implied_crisis_availability 0.464**. But the **planned/forced split is CORRECTED**: REMIT businessType shows FR nuclear unavailability is **~92 % planned / ~8 % forced**, whereas the duration heuristic (`forced_max_days=20`) inferred **~40 % forced** — it miscounts short *planned* works and economic modulation as forced. Two parser bugs fixed en route (`"Unplanned"` contains `"PLANNED"`; binding-max masks overlapping forced derates → split now message-level energy). Step-vi feed WIRED opt-in: `run_backtest(..., use_remit_nuclear_avail=True)` uses `1 − REMIT_outage_MW/nuc_cap` (via `nuclear_unavailable_mw`) instead of the rolling-max-of-output proxy; default off keeps the 2019-validated path. **→ #81 DONE (2026-07-18): forced/planned split re-parameterised to REMIT.** New config `forced_share_nuclear: 0.10` (REMIT ground truth). In `calibrate_forced`, when set, forced is a *fixed* share of the baseline (0.10·0.262 = **0.026**) instead of the residual-into-forced anchoring (which gave 0.111 ≈ 40 %); the remainder is realised as **extended planned** via a scheduler duration multiplier (`planned_duration_mult` = baseline·(1−share)/planned_cadence = **1.409**), applied in `planned_scheduler._draw_duration`. Verified: realised planned **0.235** + forced **0.026** = 0.261 ≈ baseline 0.262 → **Kd preserved** (so the #78-validated level is unchanged), implied forced share **0.10** (was 0.40). Set `forced_share_nuclear: null` to restore the old anchoring. Neighbour zones = #80 (done separately). |
| D2 | ±10 % correction target | **trend SLOPE** (default), workbook switch to level | Matches the spec's "creep on the historical trendline". `config.forced_correction_target` + per-tech `user_correction_pct`. |
| D3 | Nuclear lifetime policy | **scenario-dependent** | `fleet_registry.closure_year` + workbook `scenario` column carry closures / lifetime extensions / new builds (Flamanville 3, EPR2). |
| D4 | Interconnectors | **included** as pseudo-units | Step (vi) needs them and no other step owns them. Borders BE/DE/CH/IT/ES/GB from `rte_ntc`; planned+forced unavailability + NTC by direction. |
| D5 | 2021–23 stress-corrosion window | **excluded from baseline forced fit; used to calibrate common-mode** | Keeps the crisis a **tail event** (return period ~15–30 yr), not baked into the mean. `config.calibration.baseline_exclude`. |

## Design commitments (§2 — these decide the price tails)

- **Unit-level, not fleet-level.** Each unit is its own state process → preserves the size distribution
  of shocks (losing one 1450 MW N4 ≠ 1450 MW spread) and enables common-mode correlation.
- **Common-mode is mandatory.** A Poisson generic-event module targets a *palier* (CP0/CPY/P4/P'4/N4/
  EPR), imposes staggered correlated extended outages across a sampled fraction; calibrated so a
  2022-magnitude year has a ~15–30 yr return period. Dominates the upper price quantiles.
- **Planned-outage seasonality is economic.** EDF concentrates refuelling Apr–Sep; the scheduler must
  reproduce the observed monthly profile (workbook `month_weight_*`) or it silently flattens winter
  scarcity.
- **Heavy tails.** Nuclear overruns and forced-outage durations are heavy-tailed (lognormal/Weibull,
  never exponential-by-default).
- **Weather coupling.** River-cooled units derate in hot/low-flow summers driven by the *same* weather
  draws → heat waves raise demand (iii) and cut thermal availability (here) simultaneously.

## Phase 0 (scaffold) — done, 2026-07-10

Package `availability_model/` with pydantic `config.py`, pandera `io/schemas.py` (outage events,
availability, fleet registry, param sheets, interconnectors), `io/fleet.py` (DB-grounded registry +
French nuclear palier/cooling/basin lookup), `io/assumptions.py` (§5 template with pre-filled defaults
+ validating loader), `meta.py` (git/config/workbook/cube hashes), CLI (`init-workbook | calibrate |
project | validate`), pipeline stubs. Smoke tests pass (3/3).

Fleet registry (59 nuclear units, 65.8 GW incl. Fessenheim; 168 dispatchable units total) validated
against the DB, catching two data issues fixed in this phase:
- **Capacity via p99.9 of production, not MAX.** Single-sample spikes wreck `MAX(value)` (CATTENOM 1
  reads 32 GW; several units ~6.9 GW vs ~1.3 GW true). The p99.9 quantile lands on the rated Pmax
  because baseload units run flat at rating (CATTENOM 1 → 1308, BELLEVILLE 1 → 1312 MW). `installed
  capacities` table has **no** nuclear rows, so production quantile is the only usable source.
- **Palier lookup keyed to DB label prefixes.** RTE abbreviates "ST ALBAN"/"ST LAURENT" (not "SAINT-…")
  and Fessenheim was missing → 6 reactors had no palier. Keys corrected; Fessenheim added as CP0 with
  `closure_year=2020` (excluded from the 2027+ projection). Smoke test now asserts every reactor has a
  palier and fleet capacity ∈ (55, 70) GW so a regression can't slip through.

## Build plan (phase-by-phase, sign-off between each)

0 scaffold ✓ · 1 io + outage inference from production · 2 calibration (planned/forced/common-mode/
derating/inflow, 2021-23 → common-mode) · 3 planned-outage scheduler (nuclear-grade) · 4 forced +
common-mode processes · 5 weather derating + hydro inflows + interconnectors · 6 projection engine
(coherent draws, partitioned Parquet) · 7 validation (§7) + report + methodology.

## Phase 1 (io + outage inference) — done, 2026-07-10

Outage catalogue inferred from per-unit production for **must-run nuclear only** (`inference.inferable_
techs`). Sanity: 1633 nuclear events; technical availability **0.74 ex-crisis / 0.72 all** (target band
0.73–0.78 ✓, 2021–23 crisis correctly drags the mean down); all long events verified real (Paluel 2's
992-day 2016 steam-generator incident; crisis outages flagged `in_crisis`, 281 events). Modules:
`io/production.py` (SQL daily aggregation, coverage-gated), `io/outages.py`
(`daily_capacity_factor` / `infer_outage_events` / `availability_summary`). Tests green.

**Why nuclear-only.** Production ≈ availability only for must-run units. Merit-order peakers/mid-merit
(gas 0.53, oil 0.10, coal 0.22, pumped 0.32 "availability") are economically idle, not unavailable —
their forced/planned rates come from workbook EFOR defaults, not inference. RoR/reservoir low output is
drought (owned by res_model inflows), not an equipment outage.

**Economic idling of nuclear (design note).** Reactor modulation / load-following sits at CF ≈ 0.2–0.9,
above the 0.05 full-outage threshold, so it is counted as AVAILABLE — the inferred figure is technical
availability, not a load factor (a throttled reactor is still 100% available; step vi dispatch decides
what is called). Residual leak: *sustained* economic near-zero (multi-day hot-standby / negative-price
spells) can enter the short "forced" class ⇒ **inferred forced frequency is an upper bound**, cross-
checked vs literature EFOR in Phase 2. Partial derating (~50% output) is counted available ⇒ an
opposite-direction under-count.

## Phase 2 (calibration) — done, 2026-07-10

`calibration/` fits the parameter set from the inferred catalogue + DB and persists a pickle
(`calibrated_availability.pkl`) + JSON report. The workbook is NOT overwritten — it keeps the user
knobs (±10 % forced correction, closures, scenario) that projection layers on top (D2 layering).

- **planned** (`planned.py`) — per-palier ASR/VP/VD lognormal durations (routine only: crisis + tails
  outside the day-band dropped), refuelling cycle (median inter-outage gap), and seasonal placement
  weights. Recovers the real pattern: ASR ≈ 39 d, VP ≈ 82 d, VD ≈ 170–190 d; CPY cycle ≈ 12 mo; summer-
  heavy seasonality (CPY Nov–Jan 0.1–0.4, Aug–Sep 1.5–1.6). Thin paliers fall back to the pooled fleet
  shape + a default cycle — **EPR** (Flamanville 3, ~1 yr) flagged `reliable=False`, cycle→18 mo.
- **forced** (`forced.py`) — nuclear fitted from multi-day full outages (ex-crisis): freq 1.24/unit-yr,
  lognormal duration, calendar trend. This counts multi-day full outages only (sub-2-day trips dropped,
  partial derating counted available), so it sits below the nameplate EFOR band (2–5) — reference only;
  total unavailability is anchored by the observed Kd, not this count. Peakers use literature EFOR.
- **common-mode** (`common_mode.py`) — calibrated to the *excess* unavailability pulse above the non-
  crisis baseline (NOT raw long-outage counts, which would spuriously flag ~96 % of the fleet from
  routine VP/VD). baseline_unavail 0.28, peak_excess 0.26 → crisis trough ≈ **0.46** (real winter-2022
  low). Frequency pinned to the 15–30 yr band (D5). `target_prob` from per-palier excess outage rate
  recovers the true physics: **N4 0.64, P'4 0.25** were the hardest-hit families in 2021–22.
- **derating** (`derating.py`) — per-basin literature defaults (river/estuary sensitive at 0.03/°C >25 °C;
  sea/tower 0). A data-driven refit needs a river-temperature series (air temp alone is confounded with
  summer economic modulation) — left as a hook; the structural weather-coupling still holds via shared draws.
- **inflows** (`inflows.py`) — reservoir energy budget from RTE water reserves (capacity 3179 GWh, usable
  2593 GWh, seasonal weekly profile). RoR production stays with res_model (iv) to avoid double-count.

## Test performance (2026-07-10)

Two full-table scans (daily aggregation + p99.9 capacity) are now disk-cached (`io/cache.py`,
invalidated by DB mtime) and the test suite shares session-scoped fixtures (`tests/conftest.py`).
Suite runtime went from **1h50m → ~17 s warm / ~3 min cold**. The cache also speeds up `calibrate`.

## Phase 3 (planned-outage scheduler) — done, 2026-07-10

`projection/planned_scheduler.py` generates, per operating nuclear unit over 2027–2046, a calendar of
ASR/VP/VD outages: per-palier `cycle_months` cadence (units staggered), a VD every ~10 yr with ASR/VP
sampled at calibrated frequency between, lognormal durations, season-sampled start months, and a greedy
concurrency cap. Deterministic given (seed, draw). Diagnostics (`planned_metrics`) on draw 0:
planned unavailability **0.169** (planned + forced ≈ observed baseline 0.26 ✓), mean/max concurrent
9.6/22 (cap binds), ASR 518 / VP 400 / VD 107 (≈ one décennale per reactor per 10.5 yr), seasonality
winter 0.45–0.59 vs summer/autumn 1.2–1.26 (winter scarcity preserved). Tests green.

## Phase 4 (forced + common-mode stochastic processes) — done, 2026-07-10

- **forced** (`projection/forced.py`) — per-unit annual Poisson with rate = calibrated freq × calendar
  trend × age creep, drawn for every technology. ±10 % user correction applies to the trend SLOPE (D2,
  `forced_correction_target`). Heavy-tailed lognormal durations. A forced outage landing inside a planned
  window is dropped (unit already offline). Age creep accrues only *beyond* the calibration midpoint so
  old reactors aren't double-aged at horizon start. Verified: nuclear ≈ 1.33/unit-yr (base 1.24 + trend/
  age), peakers match their EFOR inputs, planned-suppression thins the nuclear count.
- **common-mode** (`projection/common_mode.py`) — Poisson at the calibrated ~1-in-22-yr rate; when an
  event fires, ~`peak_excess×fleet` reactors (sampled so the palier mix matches the N4/P'4-dominated
  targeting) go offline staggered for ~plateau length, reproducing the peak excess. Verified: ~59 % of
  draws see ≥1 event, peak offline fraction ≈ 0.26 (matches calibration), N4/P'4 dominate the affected
  set. Most draws are quiet; the rare event carries the price tail.

Both deterministic given (seed, draw). Assembly into a single availability series is Phase 6.

## Phase 5 (weather derating + hydro inflows + interconnectors) — done, 2026-07-10

- **thermal derating** (`projection/derating.py`) — river/estuary-cooled reactors lose output on hot days
  as a deterministic transform of the shared temperature draw (available frac = 1 − clip(frac_per_c ×
  max(0, T_lagged − threshold), 0, 0.30)); river temp ≈ lagged air temp. Verified: river units derate,
  sea/tower untouched → heat-wave demand↑ coincides with thermal↓ (the coupling that matters). National
  temperature driver (basin-specific temp is a documented refinement).
- **reservoir energy budget** (`projection/hydro.py`) — weekly available stored energy = usable capacity
  × calibrated seasonal fill profile × per-year wetness (from the shared precip draw). Dry year lowers
  the ceiling. Sets the dispatch budget only; production stays with res_model (iv) / step (vi).
- **interconnectors** (`projection/interconnectors.py`) — per border × direction daily available NTC =
  NTC − stochastic forced outages (Poisson, downtime ≈ forced_unavail) − an annual planned block
  (≈ planned_unavail). Verified: ~0.95 availability across borders, never exceeds NTC, reproducible.

All deterministic given (seed, draw). Assembly into one availability series is Phase 6, where planned +
forced + common-mode + derating are combined as a UNION per unit (offline in two categories = still just
offline; never summed).

## Phase 6 (projection engine) — done, 2026-07-10

`projection/engine.py` assembles one coherent availability series per draw: per-unit daily capacity ×
derating on hot days, then 0 on any offline day where offline = planned ∪ forced ∪ common-mode (UNION,
never summed). Weather (temp for derating, wetness for reservoir) is the shared cube — a single 20-yr
realization, so identical across draws; only the outages vary. Scenario knobs (closures / new builds via
the workbook fleet_registry, ±10 % forced correction via forced_outage_params) are layered in. Outputs
Parquet: availability_by_tech, availability_nuclear_units (with per-day state), interconnectors,
reservoir_budget + a reproducibility metadata stamp. `io/weather.py` loads national daily temp + annual
wetness from the cube. CLI `project --draws N` for quick runs.

**Kd anchoring (important calibration fix).** First projection gave nuclear Kd 0.825, far above history.
Root cause: the cadence scheduler places routine décennale/refuelling outages (~0.169 unavailability),
but history also carries extended UNPLANNED outages (long repairs) that the short-forced fit (mean 5 d)
misses — the duration-classifier had labelled those long unplanned outages as ASR/VP/VD. Fix: **residual-
anchor** the nuclear forced day-budget — `calibrate_forced` sets the forced mean duration (heavy tail) so
planned + forced reproduces the observed baseline unavailability, using the *measured* scheduler planned
(a dry-run) with a suppression gross-up (forced starting inside a planned window is dropped). Result:
Kd 0.761, centred in the 0.73–0.78 band. Common-mode still shows in the tail (event draws), not the 20-yr
mean. Tests green (29/29).

## Phase 7 (validation + report + methodology) — done, 2026-07-10

`validation/suite.py` assembles nuclear daily availability across draws (reusing the engine) and checks
§7 acceptance; `validation/report.py` writes the methodology note stamped with live numbers; `validate`
CLI wired. Result **7 PASS / 0 WARN / 0 FAIL**:
- non-crisis nuclear Kd **0.762** ∈ [0.73, 0.78] ✓
- common-mode draw reproduces the 2022 trough: worst annual Kd **0.535** vs ~0.54 target ✓
- quiet draws show no false crisis (0.676 > 0.66) ✓
- common-mode return period 22.5 yr ∈ [15, 30] ✓
- planned summer-heavy (1.24 vs winter 0.53) ✓
- availability bounded ✓; weather-derating coupling active (summer-concentrated) ✓
8/15 validation draws carried a common-mode event (~53%, ≈ the ~59% Poisson expectation).

**availability_model (step v) COMPLETE** — all 8 phases done, 33 tests green, feeds step (vi).

## Known limitations (to state in the methodology note)

- Outage history is production-inferred, not REMIT ground truth (planned/forced separated by duration;
  economic curtailment vs true outage not distinguishable — minor for baseload nuclear). See task #41.
- Extraordinary non-crisis long outages (e.g. Paluel 2's 992-day generator drop) are labelled "VD" by
  the duration rule; Phase 2 separates extraordinary tails from routine décennales.
- No endogenous maintenance-deferral in tight years (EDF shifts outages when winter is at risk) —
  a possible step-(vi) refinement.
- If step (ii) precipitation is weak, the hydro inflow model is calibrated on available weather vars
  (documented weakness).
