# DECISIONS — demand_model (step iii)

Long-term hourly power-demand model for mainland France. Hybrid statistical core
(calendar + thermosensitive + lighting + residual) + structural projection layer
(scenario rescaling + bottom-up EV/HP/H2/BTM-PV) + stochastic residual layer.

## Perimeter (§3) — fixed before coding

| # | Decision | Choice | Rationale / verification |
|---|----------|--------|--------------------------|
| P0.1 | Consumption perimeter | RTE **REALISED consommation − pumping** | REALISED is 28.6–95.1 GW = exact RTE France range. Pumping is captured as **negative `HYDRO_PUMPED_STORAGE`** (−4 GW, 45% of hours); it's a dispatch decision (step vi), so subtracted: `pumping = -min(HPS,0)`, `demand = REALISED - pumping`. |
| P0.2 | Losses | embedded | consistent with spot-price formation on the RTE perimeter. |
| P0.3 | BTM PV | **gross demand + explicit BTM-PV netting** | PV self-consumption grows in every FR scenario; modelling it explicitly (same irradiance draw) keeps the weather-price correlation and avoids double-count with step (iv). |
| P0.4 | Resolution | **hourly** (15-min REALISED aggregated to 1 h) | coarsest the spot-price model needs; matches the weather generator. Optional 15-min downscaler later. |

## Confirmed with user (2026-07-07)

- **Irradiance**: derived as **clear-sky GHI (pvlib) × cloud-attenuation** — cloud cover is available in BOTH history and the weather generator, so calibration and projection are consistent, and it doubles as the step-(iv) solar input.
- **Scenarios**: **multi-scenario** from the start (workbook `scenario` column; calibration shared, projection loops scenarios).
- Timestamps stored **UTC**; calendar features in **Europe/Paris** (DST 23/25-h days handled explicitly).

## Defaults taken (flag if you disagree)

- **Demand-side flexibility / price elasticity**: OUT of scope here → handled as flexible bids in the dispatch step (vi). The model outputs weather+calendar-driven demand only.
- **Effective-temperature weights**: population-weighted by default (stored in the workbook `weights` sheet so they can evolve); switch to consumption weights if RTE regional data is wired.
- **Heating/cooling thresholds**: estimated from data (piecewise-linear/splines), not imposed.

## Phase 1 (io loaders + QC) — done, 2026-07-07

- **Demand loader** verified on the real DB: 100 648 hourly points 2015→2026, **26.9–94.6 GW** net of
  pumping (median 49.6 GW). 15-min REALISED aggregated to hourly; pumping = −min(HPS,0) subtracted.
- **Weather loader**: 42 metropolitan stations, tidy hourly (temp/wind/cloud/humidity), tz-aware UTC;
  cloud clipped to [0,100] (upstream 101% rounding artifact); contract checked on a sample (4.2 M rows).
- **Calendar** (Europe/Paris): holidays + ponts via workalendar, DST offsets {+1,+2} detected, school
  vacation as a **national fraction** (summer/Toussaint/Christmas=1, Feb/spring≈0.6). Exact zone A/B/C
  dates flagged as a maintainable input (Éducation Nationale calendar) — approximate for now.
- **QC**: gap reindex (0.09% missing), spike removal, flat-line flag; **COVID (11 329 h) + sobriety
  (5 785 h) windows flagged and kept** as regressors (not deleted → trend not poisoned).

## Phase 2 (features) — done, 2026-07-07

- **T_nat** = station-weighted national temperature (weights from workbook `weights`, equal-weight
  fallback flagged). Real range −6.1…36.0 °C (median 12.7).
- **Thermal inertia**: EWM smoothing at ~12 h and ~60 h + lagged daily means D-1/D-2.
- **HDD/CDD**: piecewise-linear + a cold-tail term (steeper winter gradient in extreme cold);
  threshold guesses (15/20 °C) here, **re-estimated in calibration**.
- **Irradiance**: clear-sky GHI (pvlib **Haurwitz** — no turbidity-data dependency) × Kasten–Czeplak
  cloud attenuation `GHI=GHI_cs·(1−0.75·CF^3.4)`. Real GHI: night 0, summer-noon 449, max 944 W/m².
  Same function drives calibration + projection + step-(iv) solar (consistency).
- **National feature frame cached** to `models/national_features.parquet` (weather processed once).
- **Sanity**: raw winter gradient **−2.36 GW/°C** (≈ RTE ~2.4), corr(load,HDD)=0.81 — the effective
  temperature already reproduces the thermosensitivity before the GAM.

## Phase 3 (calibration) — done, 2026-07-07

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| C0.1 | Estimator | **ridge-on-splines** additive model (numpy closed-form), not pyGAM | transparent, fast on 100k×~360, and components stay **structurally separable** for projection rescaling. |
| C0.2 | Thermosensitive thresholds | **estimated** by daily grid-search: heat **14.5 °C**, cool **20.5 °C** | both land in the expected 14–16 / 18–22 bands. |
| C0.3 | No autoregressive load | excluded by design | lagged actual load can't exist 20 yr out; keeps the model projection-valid. |

Acceptance (real, holdout 2025): **winter gradient −2.30 GW/°C** (RTE band 2.0–2.8 ✓),
**bias −0.9 %**, MAPE **in-sample 2.97 %** / **holdout 3.52 %**. Components verified separable
(Σ components = prediction).

**"Chase below 3 %" refinements (2026-07-07)** — took holdout MAPE 4.14 %→3.52 % and in-sample
below 3 %, without adding any autoregressive load:

| Lever | What | Effect |
|-------|------|--------|
| Fast-response heating | `HDDfast_h{00..23}` from the 12 h-smoothed temp (morning ramp) | captures fast heating dynamics the 60 h HDD misses |
| Thermal-mass lags | `HDD_lag1/2` from D-1/D-2 daily temp | building-fabric memory across cold spells |
| Sustained-gradient fix | gradient now perturbs **all** temp inputs (T_nat/smooth/lags) +1 °C together | measured gradient −2.94→−2.30 (the split HDD terms were being under-counted; the *sustained* response is the RTE-comparable one) |
| Special days | day-before/after-holiday, Christmas week, August shutdown × hour | cuts the holiday/pont error tail |
| Post-2022 level step | permanent step at 2022-09 (crisis + sobriety + deindustrialisation) | fixes a **+1.6–1.8 % mid-sample bias** (visible when holding out 2023/2024) that a single linear trend can't span |

Holdout <3 % is **not reachable** for a lagged-load-free hourly model — hour-ahead forecasters
only beat 2 % via autoregression, which cannot exist 20 yr out. In-sample is <3 %; the ~0.5 %
in→out gap is the irreducible weather+calendar floor. The stochastic residual layer (Phase 4)
restores realistic hourly variance, and for scenario energy/peak the unbiased, correct-gradient
decomposition is what matters. Note: `holdout_years=[2024]` gives holdout 3.34 %/bias +0.27 %, so
the 2025-specific −0.9 % bias reflects continued demand decline, not a model defect.

## Phase 4 (stochastic residual layer) — done, 2026-07-08

`load(t) = m(t) + ε(t)`. The mean model m is smooth; real demand carries persistent,
heteroscedastic short-term variation. Extrapolating m alone would understate peak/tail risk
for the price step. The residual layer (`demand_model/residual/model.py`) models ε as:

    z(t) = ε(t)/σ(bucket)   ;   z(t) = φ₁z(t-1)+φ₂z(t-2)+η(t)   ;   ε_sim = σ(bucket)·z_sim

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| R0.1 | Heteroscedasticity | σ per **(season × local-hour × weekend)** = 192 buckets | winter evenings ≈ 3× summer nights; buckets derive from the timestamp alone so projection needs only a DatetimeIndex |
| R0.2 | Persistence | **AR(2)** on standardised residuals, companion-matrix stabilised | residual lag-1 autocorr ≈ 0.92 — an i.i.d. layer would look like white noise |
| R0.3 | Innovations | **bootstrap** the empirical AR-innovation pool (Gaussian optional) | preserves the fat-tailed, skewed shape of real demand shocks |
| R0.4 | Reproducibility | `numpy.default_rng(seed)`; per-draw seed in projection | audit-grade repeatable paths |

Acceptance (real, 2015–2026): **σ 1.03–3.12 GW** / global 1.96 GW; **φ=[1.215, −0.323]**
(persistence 0.89, stable); seeded self-check reproduces **lag-1 autocorr 0.915 vs 0.919** and
**std(sim)/std(emp)=1.01**. Saved to `models/residual.pkl` alongside the mean model; `calibrate`
now produces the full statistical core (mean + residual) in one command. 3 offline tests
(persistence + heteroscedasticity recovery, seed reproducibility/stability, local-time buckets).

## Phase 5 (structural projection + bottom-up) — done, 2026-07-08

`demand_model/projection/` (`weather.py`, `drivers.py`, `bottomup.py`, `engine.py`). For each
scenario × weather realization:

    load_net(t) = Σ_g D_g(year)·component_g(t) + EV + H2 + datacentres − BTM-PV + ε(t)

| # | Decision | Choice (user-confirmed 2026-07-08) | Rationale |
|---|----------|-----------------------------------|-----------|
| PJ0.1 | Heating electrification | **reshape the calibrated heating gradient** by an electric-heating index `S=(resistance+HP/COP)·renovation` — no bottom-up HP add | avoids double-counting today's electric heating; keeps the weather/°C correlation; new HPs raise sensitivity, COP+renovation temper it |
| PJ0.2 | Base evolution | **freeze the calibrated trend at the anchor year (2026)**, then scale by a composite structural index (population+tertiary/GDP+industry) net of autonomous efficiency | drivers, not the historical linear trend, carry the base forward → no double trend |
| PJ0.3 | Cooling / lighting | AC-penetration ratio / population ratio | simple, driver-linked |
| PJ0.4 | Bottom-up new loads | EV (fleet×km×kWh/km, shaped by smart/home charging archetypes) + electrolysis + datacentres + other (flat baseload) | additive, not in the historical base |
| PJ0.5 | BTM-PV netting | subtract self-consumption of **only post-anchor incremental PV** (uses the same GHI draw × PR × self-consumption ratio) | RTE REALISED already excludes today's BTM-PV → net only new capacity |
| PJ0.6 | Peak risk | deterministic peak + **residual-ensemble** peak (p50/p95) via seeded AR residual draws | single weather path today; weather-ensemble ready when weathergen emits members |

Weather cube: `weathergen/output/simulation.nc` `obs(time=175200, station=42, variable=7)`,
2027-2046 hourly; reuses the **calibration feature builder** so history/projection are identical
by construction. Outputs to `output/`: `projection_summary_<scenario>.csv` (annual energy, det +
p50/p95 peak, load factor, per-component energy) + `projection_hourly_<scenario>.parquet` for
configured scenarios.

Illustrative-workbook run (reference): **energy 466→657 TWh, peak 88.7→109.4 GW (p95 112.5)**
over 2027-46 — consistent with RTE Futurs Énergétiques electrification. *These use the placeholder
driver trajectories from `init-workbook`; real numbers await the user's scenario workbook.* 3
offline tests (anchored factors + efficiency, EV energy conservation, incremental PV netting).

## Phase 6 (validation + CLI + methodology note) — done, 2026-07-08

`demand_model/validation/suite.py` — acceptance suite writing an HTML report
(`reports/validation_report.html`), wired to `demand-model validate`. Five categories, each check a
metric + PASS / WARN(soft) / FAIL:

- **Calibration** — winter gradient in the RTE band, in-sample MAPE ≤ target, hold-out MAPE
  (soft aspiration), |bias| ≤ 2 %.
- **Components** — additive separability (Σ = prediction), heating>0 in cold, cooling≥0 in heat,
  GHI/lighting coef < 0.
- **Cold spells** — energy bias + peak error over the coldest 1 % of days, cold-spell MAPE, longest
  sustained sub-2 °C spell (thermal-inertia terms active). Actual-vs-predicted scatter embedded.
- **Residual** — heteroscedasticity ratio, AR persistence, variance reproduction, residual-surprise
  spell length (z>1σ) emp vs sim. σ heatmap embedded.
- **Projection** — per-scenario energy/peak/load-factor plausibility bounds + ensemble ≥ deterministic
  peak. Energy/peak trajectory chart embedded.

Result: **18/18 hard checks PASS, 1 WARN(soft)** — the hold-out MAPE aspiration (3.52 % vs 3.0 %),
documented as the lagged-load-free floor. Methodology note delivered as `METHODOLOGY.md` (§10).
Full CLI live: `init-workbook | calibrate | project | validate`.

## Coherent per-draw interface (2026-07-08)

The demand model is a **per-weather-scenario transducer**: one weather realization → one coherent
demand trajectory. The MC risk lives in the outer weather→demand→price loop, so the model must NOT
bake in a weather ensemble (that would average out the weather↔price co-movement).

- `Projector` (`projection/engine.py`) loads model+residual+workbook once, caches the deterministic
  net per (scenario, realization); `.trajectory(scenario, realization=k, seed=k)` = deterministic
  core + **one** residual draw, seeded off the draw index → weather k ⊕ ε k, reproducible. The price
  step draws coherent trajectories this way (residual encapsulated in demand_model, user-confirmed).
- The annual-summary peak band was **relabelled** `peak_gw_residdiag_p50/p95` and documented as a
  within-weather ε diagnostic (QC), NOT the weather-risk envelope.
- Warm-up: projection keeps the continuous 20-yr series, so the first day's lagged daily temps
  (D-1/D-2) are backfilled (immaterial) to avoid NaN in the core.

## Known limitations (to state in the methodology note)

- No explicit price-elasticity of demand (deferred to step vi).
- Single reanalysis/observed calibration record; extreme-cold thermosensitivity tail
  relies on the few cold spells in 2015–2026 + the weather generator's synthetic cold spells.
- BTM-PV and new-large-loads trajectories are exogenous (workbook), not endogenous to price.
