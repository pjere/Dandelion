# Modelling approach — end to end

> **This is the living overview of *how the whole thing works*.** It must be updated in the same change
> as any behavioural edit to the models (see [CONTRIBUTING.md](../CONTRIBUTING.md)). Per-step detail lives
> in each package's `METHODOLOGY.md`; the design decisions behind each choice live in the per-package
> `DECISIONS.md` and in [docs/ADR.md](ADR.md). This document is the map that ties them together.

## 1. What the project computes

A bottom-up, weather-driven simulator of **hourly electricity spot prices** for France (and five European
neighbours), usable both as a **backtest** against historical years and as a **20-year projection**
(2026–2046) under capacity and climate scenarios. Prices are formed the way the real market forms them —
by clearing supply against demand on an economic merit order — rather than fitted directly. Everything
above the raw market data is reconstructed from physical and economic first principles so the model can
be pushed into futures (high-RES, electrified demand, retired thermal fleets) that no historical
regression could reach.

The chain is a sequence of independent, individually validated packages. Each is one "step" (numbered
ii–vii, following the original design memo); each consumes the previous steps' outputs through shared
data artifacts, never through hidden coupling.

```
        ii                iii            iv              v                 vi                 vii
   ┌───────────┐    ┌───────────┐  ┌──────────┐   ┌──────────────┐   ┌────────────┐   ┌────────────┐
   │ weathergen│───▶│  demand   │  │   res    │   │ availability │   │  dispatch  │──▶│   markup   │
   │  weather  │─┬─▶│  (load)   │  │ (wind/PV)│   │ (fleet up/   │   │  7-zone LP │   │  SMC→spot  │
   │   cube    │ │  └─────┬─────┘  └────┬─────┘   │  down time)  │   │  → prices  │   │  (in disp.)│
   └───────────┘ └────────┼─────────────┼────────▶└──────┬───────┘   └─────┬──────┘   └─────┬──────┘
        │                 │             │                │                 │                │
        └── same draw ────┴─────────────┘                │                 │                │
                                                          ▼                 ▼                ▼
                                              net load, RES potential,  system marginal   day-ahead
                                              firm availability  ─────▶  cost (LP duals) ─▶ spot price
```

The shared substrate underneath all of it is **`powersim_core`** (glossary, RNG authority, the Parquet
lake + DuckDB catalog, the scenario workbook accessor, serialization, the weather-cube loader) and the
**`pricemodeling`** ETL that lands all the raw market/weather/registry data into one SQLite `master_hourly`
base. See [ARCHITECTURE.md](ARCHITECTURE.md) for the module map and the data-flow diagram.

## 2. Step ii — `weathergen`: the stochastic weather cube

A multi-site hourly **weather generator** fitted to ~12 years of French SYNOP observations (42 stations)
and ERA5 reanalysis. It produces `simulation.nc`, an `xarray` cube `(time, station, variable)` of
physically plausible synthetic weather over the 20-year horizon — temperature, wind (10 m **and** 100 m),
humidity, pressure, precipitation.

Key choices (detail in `weathergen/METHODOLOGY.md` / `DECISIONS.md`):
- **EVT marginals** (semi-parametric, generalized-Pareto tails) so extremes *extrapolate* beyond the
  observed record instead of being capped at historical maxima — essential for cold-snap / heatwave price
  events.
- **Dependence** via a Gaussian copula plus an EOF-VAR latent field, so spatial correlation (a cold, calm
  day is cold and calm nationwide) and temporal persistence (spell length) are reproduced, not just
  marginals.
- The **climate-change trend is imposed externally** (CMIP6 quantile deltas, quantile-delta mapping),
  never estimated from the short record — tail intensification comes from the climate model.
- 100 m wind is **co-generated** with 10 m via a fitted transfer so wind-power conversion downstream sees
  hub-height wind coherent with the surface field (see `weathergen/WIND_TEMP_COUPLING.md` for the
  temperature-coupling investigation and why the shipped model is transfer-only).

One RNG realization of the cube is the single weather input shared by demand and res, so their draws are
**coherent** (the same weather drives both load and renewables).

## 3. Step iii — `demand_model`: hourly French load

A **hybrid statistical–structural** long-term demand model. A calibrated statistical core decomposes
historical load into components (thermosensitive heating/cooling, calendar, base, lighting) with an
hourly temperature-response shape; a **structural projection layer** then evolves the base forward with
bottom-up drivers that a pure time-series model cannot see: heat-pump stock and its COP-vs-temperature
curve, EV fleet and charging profiles, electrolysis / datacentre point loads, efficiency gains,
behind-the-meter PV. A stochastic residual layer adds the correlated noise. Consumes the weathergen cube
for the temperature/irradiance drivers. Detail: `demand_model/METHODOLOGY.md`.

## 4. Step iv — `res_model`: weather → renewable production

Calibrated **conversion chains** turning the same weather draw into hourly **potential** production for
PV (utility + distributed), onshore wind, offshore wind (fixed + floating) and run-of-river hydro. A
transfer layer bridges station-level weather to ERA5-100 m hub-height wind and clear-sky-model irradiance;
the chains are calibrated to national capacity factors and their distributions; a stochastic residual
layer preserves the demand↔RES correlation structure. Because demand and res consume the **same**
weathergen realization, a cold, calm winter hour is simultaneously high-load and low-wind in the coupled
draw — the physical driver of scarcity pricing. Detail: `res_model/METHODOLOGY.md`.

## 5. Step v — `availability_model`: stochastic fleet availability

Unit-level **availability** of the French dispatchable fleet — the supply-side twin of demand's weather
risk. It models planned outages (nuclear refuelling/decennial cadence with a concurrency-capped
scheduler), forced outages (heavy-tailed durations), common-mode events (the 2022 stress-corrosion crisis
as the reference trough), weather derating (river-temperature limits on nuclear, hydro inflows) and
interconnector availability. Calibrated against **REMIT** outage disclosures (the market's own outage
feed, ingested by `pricemodeling`), with the forced/planned split re-parameterised to the ~8–10 % REMIT
share rather than inferred from production. Produces per-draw hourly available-MW trajectories that the
dispatch consumes. Detail: `availability_model/METHODOLOGY.md`.

## 6. Step vi — `dispatch_model`: the 7-zone economic dispatch

The price-formation core. A continuous **linear dispatch** (linopy/HiGHS, no unit commitment) over a
7-zone European footprint — France **unit-resolved** (≈170 units, SRMC = fuel/η + CO₂·intensity/η·EUA +
VOM), the neighbours DE-LU / BE / CH / IT-North / ES as aggregated technology-block stacks, a virtual
DE-REST export sink (NL+AT+DK+PL+CZ), and GB as a border supply curve — coupled by NTC-bounded
cross-border flows. Solved over rolling weekly windows.

The **price of each zone is the dual of its energy-balance constraint** (the system marginal cost, SMC).
Scarcity, negative prices and cross-border spreads are therefore *duals, never post-processed*: unserved
energy prices at VoLL, RES over-generation at the price floor, and — the negative-price mechanism —
must-take renewables bid a **subsidy-scheme supply curve** (paid-regardless FiT deep floors, sliding
market premiums, merchant ≈0) with the German **§51 EEG trigger** cancelling premiums after N consecutive
negative hours, solved as a fixed point. Hydro reservoirs get weekly energy budgets from historical guide
curves, and their water value is the dual of the budget cap. Detail: `dispatch_model/METHODOLOGY.md`.

## 7. Step vii — the SMC→spot markup (the "wedge")

The LP returns *marginal cost*; real **day-ahead spot** sits above it on average and is more volatile
(unit-commitment start-up/no-load recovery, scarcity rents, downward decoupling in surplus). Step vii
(`dispatch_model/markup.py`) fits that wedge — `spot = SMC + markup(drivers)` — as a **sign-constrained
ridge regression on projectable structural drivers only** (SMC level, system tightness = residual demand
/ firm capacity, RES share, hour/month harmonics — **never** a calendar-year dummy, which could not
extrapolate to 2040). Economic sign constraints keep the wedge non-decreasing in price and tightness so
it degrades gracefully in the high-RES/high-price 2040 regime instead of extrapolating absurdly. Fitted on
a multi-regime panel (2019 normal + 2022 gas crisis + 2023) with a quality gate that drops zone-years the
dispatch prices badly. Detail: `dispatch_model/STEP_VII_METHODOLOGY.md`.

## 7b. The learned marginal-tranche surrogate (opt-in, under construction)

A 20-year hourly trajectory costs ~11 min of LP solving, which bounds how large a weathergen Monte-Carlo
can be. The surrogate (`dispatch_model/surrogate/`) aims to predict **which tranche is marginal** and map
that to a price analytically, **deferring to the exact LP whenever it is unsure**. The LP path is
untouched and remains the default; the surrogate is opt-in and gated on beating LP+markup on held-out data.

Four decisions define it, and each one is a guard against a way this could quietly go wrong:

- **The target is latent.** ENTSO-E publishes prices, generation per production type, load, flows and
  REMIT outages — never "which unit set the price". So the label is *constructed*: the Δ-generation
  response is the **filter** (which plant could physically be marginal — it moved with residual demand),
  and proximity to the observed clearing price is the **selector**. Every label carries a `confidence`
  used as a sample weight; ambiguous hours are down-weighted, not silently guessed. Ranking movers by
  *magnitude* rather than *cost* was measured and rejected (it finds the fleet's shock-absorber, not the
  price-setter: ~48 % of FR hours mislabelled nuclear, 24-52 €/MWh implied-price error).
- **Labels live on the price-coupled area, not the zone.** France shares an identical clearing price with
  a neighbour in ~61-65 % of hours, so "which *French* tech is marginal" is wrong by construction there.
  Pooling the coupled area's merit order lifted FR label agreement 68 %→90 % (2019).
- **Features are projection-available and expressed as ratios.** Observed price and observed generation
  are *forbidden* as inputs (`features.assert_no_leakage` enforces this) — they do not exist in 2046.
  Tightness, RES share and fuel *spreads* recur across regimes where absolute megawatts never do, so a
  2046 hour can land in seen ratio-space. The strongest single feature is `srmc_at_residual` — where
  residual load sits on the zone's own supply curve — which turns the task into learning the *corrections*
  (coupling, ramp, scarcity) rather than a price level from scratch.
- **Splits are by year, never by hour**, since neighbouring hours are near-duplicates: train 2019 + 2022 +
  2023 (normal / crisis / transition), hold out 2024 untouched.

**Switzerland is excluded from training.** It is hydro-dominated, and hydro's opportunity cost is an
endogenous water value (the LP's budget dual), not an SRMC — so an SRMC-derived label cannot represent it.
Measured: 15-23 % agreement and 37-104 €/MWh error, against 5-7 €/MWh elsewhere. Including it would train
the model on noise; fixing it needs a water-value head, which is deliberately out of scope for now.

### Outcome: PARKED, and why (2026-07)

The surrogate was built through the held-out-2024 price gate and **did not clear it**. The code remains in
the tree, fully tested, behind a flag that is **not enabled**; the LP is and remains the only price path.
Recorded here so the result is not rediscovered at cost:

| held-out 2024, MAE €/MWh | label_oracle *(ceiling)* | merit_order *(no ML)* | surrogate_crf | surrogate_flat |
|---|---|---|---|---|
| BE | 29.5 | 32.1 | **29.4** | 30.7 |
| DE_LU | 20.8 | 37.4 | 24.3 | **22.3** |
| ES | 21.9 | **28.1** | 32.6 | 34.8 |
| FR | 24.9 | **38.3** | 32.9 | 35.6 |
| IT_NORTH | 25.9 | **16.7** | 17.9 | 17.7 |

1. **The ceiling is too low to be worth chasing.** `label_oracle` — the price implied by the *true* derived
   label — still errs by 21-30 €/MWh. That is label quality, not model capacity, so no amount of training
   closes it. In IT_NORTH the oracle (25.9) is *worse than the trivial merit order* (16.7).
2. **It loses to a no-ML baseline in 2 of 5 zones** (ES, IT_NORTH), where reading residual load off the
   supply curve simply beats the trained model.
3. **The Markov structure did not clearly earn its place.** Tranche accuracy was 59.9 % against a 56.1 %
   majority-class floor, and the *best* accuracy in the whole sweep (61.7 %) came from the **chain-free**
   variant. The CRF did win on price in 3 of 5 zones, which is the metric that matters — but from a level
   that fails anyway.
4. Negative and scarcity hours behaved as predicted: unrecoverable from a tranche label (scarcity MAE
   150-300 €/MWh, and positive bias on every negative-price hour), confirming they must defer to the LP.

**A methodological trap worth remembering**: the training split was briefly filtered to confidently-labelled
hours. That punched holes in the hourly grid, sequences broke at every hole, FR training fragmented to a
median 9 h block, most data was silently discarded, and the CRF's partial-supervision path never ran —
holdout accuracy 38.8 % vs 60 % once fixed. `dataset.split` now keeps every hour and a regression test
guards it.

**If resumed**, the first move is label quality, not architecture — the oracle ceiling is the binding
constraint. Hydro water value (which is why CH was dropped) and a setting-zone head are the obvious gaps.

## 8. Backtest vs projection

- **Backtest** (`dispatch-model backtest`) clears a **historical** year against ENTSO-E actuals (real net
  loads, observed reservoir energy, REMIT nuclear availability) and scores the §8 price metrics —
  baseload error, quantile errors, correlation, negative/spike frequency — against observed spot. This is
  the acceptance gate; the 2019 baseline is frozen in the golden harness.
- **Projection** (`dispatch-model run` / `rolling.projection`) clears a **future** year: it takes a
  reference-year hourly weather shape (or a re-drawn weathergen realization via the `weather_shapes` hook)
  and evolves the *structure* forward — capacity from **TYNDP** trajectories (with a flexibility fleet for
  2040 adequacy) or a CAGR fallback, the year-varying RES subsidy bid stack (support roll-off, §51 trigger
  tightening 6h→1h), forward commodity prices — then applies the markup. Output: per-zone hourly spot
  trajectories 2026–2046.

## 9. Cross-cutting engineering (what makes it reproducible)

- **`powersim_core`** is the single authority for: the naming glossary ([CONVENTIONS.md](../CONVENTIONS.md)),
  the RNG (`SeedSequence` keyed by draw id — same seed + config ⇒ identical output, collision-free across
  processes), the Parquet **lake** + DuckDB **catalog** (all model outputs), portable **JSON+npz model
  serialization** (never pickle), and the single hand-edited **`scenarios.xlsx`** workbook.
- **Golden harness** (`tools/golden.py` + `golden/baseline.json`) freezes 13 model outputs by numerical
  stat-digest; `python tools/golden.py check` gates every change so a refactor cannot silently move a
  number.
- Architecture decisions are recorded in [docs/ADR.md](ADR.md); the full Phase-0/1 and Phase-2 code
  reviews in [REVIEW.md](../REVIEW.md).

## 10. Honest limitations (do not oversell the output)

- Neighbour zones use **reduced-form** weather-response models (load ~ FR national temperature, RES ~ FR
  national CF), not station-resolved weather — a full build would extend weathergen and the demand/RES
  models to each neighbour.
- The headline 2026–2046 projection is a **deterministic central path on a fixed reference-year weather
  shape**, not a weather-ensemble distribution. The machinery to run ensembles is in place — the
  `weather_shapes` hook + per-draw REMIT availability, and a **parallel Monte-Carlo harness**
  (`dispatch_model/rolling/montecarlo.py`) that runs draws across cores byte-identically to serial — but
  the *headline* figures quoted here are one central path, not a distribution.
- **IT-North** markup quality is poor (negative R²) — a dispatch-side problem (IT scarcity premium / gas
  basis), documented not hidden.
- 2040 capacity is a **starter TYNDP trajectory** (editable in `scenarios.xlsx`), and raw RTE/ENTSO-E
  re-pulls are **not bit-reproducible** because the sources revise published history.
