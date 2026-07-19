# DECISIONS — dispatch_model (step vi)

Multi-zone economic dispatch → hourly zonal marginal prices. 7 bidding zones (FR unit-resolved; DE-LU,
BE, GB, CH, IT-North, ES aggregated), NTC-coupled. Consumes steps (ii)–(v) + a commodity module. Feeds
step (vii) which maps system marginal cost → day-ahead spot via a calibrated markup layer.

## Data reconnaissance (2026-07-14) — what the DB had vs what step vi needs

**Had (FR side):** demand (iii), RES (iv), unit availability (v), `rte_ntc` (French borders, 2021+),
`rte_physical_flows`, French weather cube (42 FR stations).

**Missing → provisioned via ENTSO-E:** the DB had **no usable spot prices** (`rte_market_prices` = 96 rows
of RTE last-resort/imbalance prices for 28-29 Jun 2026, €15k/MWh — not EPEX day-ahead), **no neighbour
history**, **no commodities**. The user supplied an **ENTSO-E Transparency token** (in `.env`,
gitignored); token verified live (FR day-ahead 2024-01-15 = 77.7, 72.1, 66.8… €/MWh).

## Blocking decisions

| # | Question | Choice |
|---|----------|--------|
| E1 | Backtest/neighbour data | **Ingest from ENTSO-E** (prices, load, generation-per-type, flows) for the 7 zones via `entsoe-py` → long-schema DB tables (`entsoe_*`). |
| E2 | Foreign weather anchor points | **Ship fallback** neighbour demand/RES regressions on FR-station predictors in v1 (quantify correlation loss for ES/GB); step-(ii) anchor-point extension is a later refinement. |
| E3 | Commodities | ENTSO-E does **not** provide TTF/EUA/API2/Brent → commodity module is **workbook-driven** (annual trajectories + monthly shape), stochastic OU layer behind a flag. History source TBD (deferred). |
| E4 | Build order | **Degraded 1-zone mode first** (FR unit-level + parametric border supply curves) end-to-end, then expand to 7 zones. Codebase zone-agnostic (`run.mode: single_zone | multi_zone`). |
| E5 | Optimisation | **Linear** dispatch (relaxed commitment) via **linopy + HiGHS**, not MILP. Prices = duals of zonal balance; scarcity priced with DSR tranches + VoLL slack + curtailment bids so negative/scarcity prices are endogenous. |
| E6 | Hydro | Start with **guide curves + weekly energy budgets** (option b); keep interface for SDDP water-values (option a). |
| E7 | Time resolution | **Hourly LP**; 15-min shaping deferred to step (vii). |
| E8 | Reserves | Static reserve-margin deduction per zone (v1); co-optimisation is a v2 refinement. |

Toolchain installed & verified: `entsoe-py 0.8.0`, `linopy 0.8.0`, `highspy`.

## Phase 1 (ENTSO-E ingestion) — in progress

`pricemodeling/entsoe/series.py` (extends the pre-existing `prices.py`): ingests day-ahead prices, load,
generation-per-type, and cross-border flows for the 7 zones into long-schema tables via the shared db
helpers. Yearly chunks + `ingest_log` = idempotent/resumable; per-chunk retry with backoff on 503/5xx/429
(ENTSO-E is flaky) and graceful continue-on-error. Backfill years: 2019 / 2022 / 2023 / 2024 (normal /
crisis / high-RES). Verified on Jan 2024: FR load 51.6 GW, 12 PSR types, DE_LU at native 15-min, flows
both directions. `dispatch_model/io/entsoe_hist.py` reads these back (resampled hourly, PSR→tech mapped).

## Phase 0 (scaffold) — done

`dispatch_model/` package: zone-agnostic `config.py`/`config.yaml` (7 zones + coupling graph + single-zone
mode), `meta.py` (hashes), `io/schemas.py` (zonal prices/dispatch/flows contracts), `io/entsoe_hist.py`
loaders, `cli.py` (build-inputs | run | backtest | validate), pipeline stubs.

## Phase 1 (ENTSO-E ingestion) — done

Backfilled prices / load / generation (5.3M rows) / flows for 2019/2022/2023/2024. Final log: **155 ok,
0 error, 9 nodata**. Every remaining gap is **GB** — post-Brexit, Great Britain is no longer published on
ENTSO-E Transparency (moved to Elexon/BMRS). **Decision:** model **GB as a border supply/demand curve**
(import/export tranches with a post-Brexit friction), not a full unit-modelled zone — consistent with GB
being non-coupled anyway. Sourcing GB from BMRS is a possible later refinement. The other 6 zones are
fully ingested.

## Phase 3 (neighbour modules) — backtest mode done

`neighbours/blocks.py`: per foreign zone, `build_neighbour_stack` (aggregated tech blocks; thermal split
into efficiency sub-blocks for supply-curve slope; capacity = p99 of observed generation) + `neighbour_
netload` (load − must-take RES, from ENTSO-E actuals). Verified: DE_LU 65 GW (lignite 16.8 / coal 15.7 /
gas 11.8 / nuclear 9.5), ES gas-heavy 36.5 GW; net loads in the right bands; and **German fuel-switching
emerges endogenously** — 2019 coal≈gas≈lignite (~€45), 2022 gas €340 »  coal €147 / lignite €159. Tests
green. **Remaining:** projection-mode modelling (demand weather-regression on FR-station fallback, RES CF
transfers, workbook TYNDP/ERAA capacity trajectories) — needed for 2027-2046, not for the backtest.

## Phase 2 (commodities) — done

`commodities/model.py`: gas/CO2/coal/oil monthly generator. Deterministic = annual level (interpolated)
× seasonal shape (gas winter premium); stochastic = correlated OU log-deviations behind a flag, seeded
per draw. Backtest-year levels seeded from public annual averages (2022 gas €123, EUA €81 …). One source
of truth for FR + neighbour stacks. Monthly commodity history is a documented refinement. Tests green.

## Phase 4 (FR unit-level stack) — done

`io/fr_fleet.py` (FR dispatchable units + p99.9 capacity, disk-cached) + `stacks/costs.py` (SRMC =
fuel/eff + CO2·intensity/eff·EUA + VOM; per-unit efficiency dispersion → mid-merit slope) + `stacks/
fr_stack.py`. 168 units / 92.4 GW. Verified merit order and — critically — **endogenous fuel switching**:
under the 2022 gas shock the order flips to coal €140 < oil €231 < gas €339. Tests green.

## Phase 5 (LP core, single-zone) — done

`lp/single_zone.py` (linopy/HiGHS): least-cost dispatch vs net load; price = **dual of the balance
constraint**. Scarcity priced inside the LP (DSR tranches as high-SRMC units, VoLL slack, RES res_bid,
over-gen dump at the floor) so negative/scarcity prices are endogenous. Validated across all regimes:
floor −500 (deep oversupply) · −10 (RES curtailment) · 7 (nuclear) · 60 (gas) · 200 (oil) · 1000 (DSR) ·
15000 (VoLL); exact energy balance. Multi-zone NTC coupling extends this in the 7-zone step.

## Milestone — first real FR prices end-to-end (2026-07-14)

Ran the full chain on FR Jan-2019: `master_hourly` demand + must-take RES → net load → FR stack (SRMC
from 2019 commodities) → single-zone LP. First pass (autarky) blew the mean to €632 via false VoLL spikes
(FR imports in tight winter hours). Adding a **border supply curve** (import tranches as high-SRMC pseudo-
units — the spec's 1-zone-mode remedy) fixed it:

| metric | model | observed |
|---|---|---|
| mean | 37.6 | 61.2 |
| P50 | 43.7 | 62.8 |
| P95 | 48.0 | 83.9 |
| corr (hourly) | **0.712** | — |

The residual ~€20 level gap is fairly parallel across quantiles → the systematic **SMC→spot markup that
step (vii) calibrates** (uplift/ramping/bidding), not a model error. Confirms the chain (steps iii→iv→
stack→LP→duals) is sound. Border supply curve is now a required feature of single-zone mode; availability
here used a documented proxy (nuclear from rolling-max actual output, thermal 0.9, reservoir water-value
€40) pending REMIT (task #41).

## Phase 5b (multi-zone NTC-coupled LP) — done

`lp/multi_zone.py`: N zonal energy balances linked by NTC-bounded **directed** flows (fwd/bwd + tiny
gross-flow ε to kill loop flows). Each zone's price = its balance dual; spreads form endogenously. Per-
zone hydro energy caps + water values carried through. Validated: NTC-binding → decoupled prices + spread
+ flow pinned at NTC (cheap→expensive); ample NTC → prices converge to one system marginal. Zone-agnostic
(works for any {zone: stack, netload} set). GB enters as border tranches on FR/BE, not a balance.

## Phase 6 (hydro coordination) — reservoir done

Two-level decomposition, **option (b)**: `hydro/guide_curves.py` derives the weekly reservoir energy
budget from the historical seasonal generation profile (`master_hourly` prod_hydro_water_reservoir),
scaled by annual wetness; `rte_water_reserves` kept as the stock guide curve for a later SDDP swap
(option a). The LP (`energy_caps` param) caps reservoir generation to the weekly budget; reservoir is bid
at ~0 so it **self-allocates to peak hours (peak-shaving)** and the **water value emerges as the dual of
the budget cap** (verified = the €60 gas it displaces) — no more €40 placeholder. Tests green (budget
binds, peak-only dispatch, water value = displaced tech, DB climatology winter>summer).

**Remaining in this phase:** PSP round-trip storage arbitrage (charge/discharge state within the window)
and coarser CH/ES/IT-North reservoir budgets — refinements on top of the working FR reservoir mechanism.

## Milestone — first real 7-zone prices (2026-07-14)

`rolling/assemble.py` wires FR unit stack + 5 neighbour block-stacks + GB-as-border-curve + flat NTCs +
hydro budgets into the multi-zone LP. Double-count traps handled (ROR/solar/wind = must-take; PSP excluded
v1; reservoir budget = window's actual reservoir energy). First real 6-zone week (2019-01-14/21) solved in
0.2 s:

| zone | model | obs | corr |    | zone | model | obs | corr |
|---|---|---|---|---|---|---|---|---|
| FR | 49.9 | 62.1 | 0.53 | | CH | 50.6 | 62.7 | 0.58 |
| DE_LU | 48.4 | 48.5 | 0.55 | | IT_NORTH | 51.4 | 68.8 | 0.62 |
| BE | 49.9 | 57.9 | 0.54 | | ES | 49.1 | 65.7 | 0.84 |

VoLL scarcity 0 h, FR–DE spread sign-match 74%, DE_LU nearly exact. **First-pass fix that mattered:**
neighbour capacity from p99→**p99.9** of generation (p99 badly undersized peakers → false VoLL) + a **GB
import block** on FR. Residuals: (1) ~€12-15 low offset = SMC→spot markup (step vii); (2) under-
differentiation between zones from flat NTCs → **Phase 8** wires real time-varying NTCs (`rte_ntc`/ENTSO-E)
and ENTSO-E installed capacity. The multi-zone assembly + coupling is proven.

## Phase 8 (backtest + methodology) — first full-year results

`rolling/backtest.py`: preloads a year once, solves the multi-zone LP over ~52 weekly windows, scores §8
price metrics per zone vs observed ENTSO-E prices, writes Parquet + metrics CSV. **DSR tranches**
(300/1000/4000 €/MWh as high-SRMC pseudo-units per zone, spec §2) added — they step the price below VoLL
and were the fix that collapsed false-scarcity means (855% → sane). `METHODOLOGY.md` written (LP
formulation, hydro, simplifications+signs, backtest table, step-vii contract).

**Installed-capacity fix (big win):** neighbour stacks now sized from **ENTSO-E installed capacity ×
availability derating** (`ingest_installed_capacity` → `entsoe_installed_capacity`; `load_installed_
capacity`), not the p99.9-of-generation proxy which undersized peakers (DE gas 11.8→31.7 GW → DE was
over-priced). Full-year 2019 annual baseload went to **FR −4.3%, DE +4.4%, BE −4.0%, CH +0.6% (4/6 within
±5%)**, IT −19.7%, ES −19.3%; **correlations jumped to 0.71–0.74 (FR/DE/CH)** from ~0.4. P50 ≈0.

**Flow-derived NTC:** `flow_derived_ntc` sets per-border/direction NTC = p99.5 of realized physical flow
(congestion-reflecting; FR→IT 3.1 GW not 4.35, DE→BE 0.17 GW). CH → −0.3%; spreads now data-grounded.
Final 2019: **FR −3.8, DE +4.5, BE −3.5, CH −0.3 (4/6 within ±4.5%)**, corr 0.60–0.74.

Remaining residuals = calibration signal, not bugs: (1) parallel level gap + neg-P95 ≈ **step-vii markup**;
(2) **IT-North/ES −19% is NOT interconnection** (flow-NTC didn't move it) — they burn gas above TTF (PSV/
MIBGAS +€2–5/MWh_th) + larger day-ahead premium → zone gas premium + step vii (deliberately not ad-hoc-
fitted here). **Remaining:** projection-mode neighbours, 50-draw projection engine + Parquet, structural/
physics metrics (per-tech gen ±10%, net-export ±15 TWh), zone gas hubs.

## §8 projection-sensitivity checks — PASS

`assemble_window(price_mult=, nuc_avail_mult=)` perturbs commodities/availability; monotone checks on a
2019 winter week (tested in `test_sensitivity.py`): **gas +50% → prices up, FR more (gas-marginal), spread
widens**; **CO2 +50% → prices up, DE more (coal/lignite-heavy → higher CO2 intensity), spread narrows** —
correct fuel-mix physics; **nuclear −30% → FR price explodes, DE flat → FR premium** (common-mode-year
signature). Confirms the model's forward behaviour is directionally sound for the projection.

## Availability source for the backtest — settled

FR unit availability for backtests: **ENTSO-E REMIT** (`query_unavailability_of_generation_units`,
confirmed accessible — task #41) as ground truth once ingested, with step-(v) inference as fallback.
Supersedes the earlier "cap thermal at observed generation" idea.

## Out of scope (documented, handled by step vii calibration)

Intra-zonal grid, reserve co-optimisation, unit-commitment combinatorics, strategic bidding, FB market
coupling (plain NTC used). The SMC→spot gap is step (vii)'s calibrated markup/spread layer.
