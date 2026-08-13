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

> **SUPERSEDED.** The "later refinement" was taken: `pricemodeling.elexon` sources GB generation, demand
> and prices from BMRS, `pricemodeling.fx` converts BMRS's GBP to the lake's EUR via the ECB reference
> series, and GB is now a modelled zone with a balance and four borders. The trigger was not GB itself but
> the Netherlands: per-border NTCs left NL over-priced by €17.6/MWh because the model gave it two borders
> where reality has five, and BritNed could not be added until GB existed. The import tranches this
> decision created (`GB_IMP1`/`GB_IMP2`, 4000 MW on the FR stack) were removed in the same change — they
> are exactly the FR-GB NTC, and keeping both would have doubled the Channel.


### GB's two feeds are measured on different boundaries — and must be reconciled

Promoting GB exposed a defect no ENTSO-E zone can have. Every other zone's load and generation come from
the same TSO submission on the same boundary, so they balance by construction. GB's do not: Elexon's
`demand/outturn` reports **ITSDO**, demand at the *transmission* boundary and therefore already net of
distribution-connected plant, while FUELINST and AGWS meter *transmission-connected* plant. Measured on
2024 the balance is short **5851 MW — a fifth of GB demand**.

Left uncorrected this is not a cosmetic error: GB ran out of plant and hit VoLL in **612 hours against 36
observed**, and because BritNed couples it directly to a tight Netherlands, NL was dragged to 15 000
EUR/MWh in **97** of them, taking its mean error from −1.4 to **+29.0 EUR/MWh** — while Belgium and Germany
sat at 126–300 EUR/MWh in the same hours. One defect, one zone, not a distribution-wide drift.

**Decision:** reconstruct the residual hour by hour and net it off GB demand (`io/gb_embedded.py`), as a
month × hour-of-day median. It decomposes by measurement into an exact **solar double-count** (AGWS reports
national solar, which GB metering has already removed from ITSDO: regression coefficient −1.25, and
corr(residual + solar, solar) = −0.065) plus a flat **~7.3 GW embedded firm block** — CHP, waste, small
gas. Both are fixed by the same subtraction, since `(load + solar) − (residual + solar) ≡ load − residual`.

Netted off **demand** rather than added as **supply**, deliberately: as must-take supply at zero it would
manufacture negative prices GB does not have, whereas heat-led embedded CHP does not respond to price
anyway. Two consequences are accepted and recorded: GB's own stack adequacy is no longer tested by the
model, and the term absorbs Elexon's evolving feed coverage as well as Britain's embedded fleet (the
residual is 8.4–29.7 % of demand depending on year, and 2025's low reading tracks an AGWS wind revision
rather than a physical change). Both are acceptable because GB exists in this model to be a correct
**neighbour** for FR/BE/NL, not to have its adequacy assessed.

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
(works for any {zone: stack, netload} set). Every configured zone carries a balance, GB included since its
promotion (see the superseded Phase-1 decision above).

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

## Lazy rows — TRIED, MEASURED, REJECTED (2026-08)

Deferring the rarely-binding FLEX row families and adding them back only where violated. Exact by
construction: removing rows relaxes an LP, so a reduced solution violating none of the omitted rows is
optimal for the full problem and the omitted rows' duals are genuinely zero. Built to the letter — pure
per-family builders (kept, `_fam_*`), a round loop, a violation check, a round-cap fallback to the full
build of the same spec variant, and a mandatory invariant asserting every omitted row is satisfied.

**It works, and it is not worth it. Measured 1.16x.**

    one arm per process, warm-then-measure, 6 backtest windows of 2024
    full build   182.14 s   147 153 rows
    lazy         156.65 s   106 953 rows   (-27.3 % rows, 16 % faster)

The deferred set came from a dual census, not from expectation: over 10 backtest and 10 projection-2034
windows, C2a binds on 0.016 %/0.243 % of its rows and rho on 0.190 %/0.010 %, against C1a at 16 %/43 %.
C2a never added a single row in any window measured.

**Why 27 % fewer rows buys only 16 %: presolve was already doing it.** This repo measured presolve as
worth >20x on this model, and rows that never bind are exactly what presolve eliminates. The census
selected C2a and rho *because* they never bind — i.e. precisely the rows presolve was always going to
strip. The driver reimplements presolve's job in Python and pays extra rounds for it.

**A BEWARE for anyone reaching for this again.** An earlier measurement showed 3.90x and was wrong: both
arms ran in one process with the full build first, so it paid ~600 s of cache warming (`framecache`, the
`lru_cache`s, `_REGIME_NTC_MEMO`) that the lazy arm inherited for free. Two cold runs of identical work
measured 573 s and 455 s here — a 26 % spread — so ANY A/B on this codebase must run one arm per process
and compare warm runs.

### The finding that outlives it: the golden is not robust to solver perturbation

Lazy rows are exact PER WINDOW — replayed against a fixed seam state, objectives matched to 0.0000 with
zero violated rows across all 147 513 constraints. Across a SEQUENCE they are not, and the cause is not
the lazy rows:

* the LP has a large degenerate optimal face. Two solutions of one window differed in 2276 columns —
  49 GWh of Spanish generation, 20 GWh across ES>PT — at an objective difference of 5.3e-16, every
  block's cost-delta cancelling to zero;
* `flexibility.tail_state` carries the window's PRIMAL into the next window's seam (u_init/p_init/
  d_hist), so an arbitrary vertex choice becomes a genuinely different LP downstream. Measured: 5.3e-16
  in window 1 became 2.9e-05 in window 2.

So ANY perturbation moves the artifact: a HiGHS upgrade, a presolve change, a different platform. That is
true of the tree today, independent of lazy rows.

**A border tie-break was tried against this and REJECTED.** Extending the F6/F8 deterministic-perturbation
technique to per-border flow costs (`_EPS_FLOW` + up to 1e-4, keyed per border and direction) left the
divergence at 2.898e-05 unchanged, left the primal delta at ~4950 MW, spread the price difference from 14
cells to 132, and shifted one window's objective by 179. The reason it cannot work: the degenerate
direction is INTER-TEMPORAL, not inter-unit. Two flexible units swapping output between two hours cost
exactly the same because SRMC does not vary by hour, and no per-unit or per-border perturbation — both
constant in time — can separate that. Breaking it needs an hour-dependent cost perturbation, which
distorts the intra-day merit order.

## Four metered technologies belonged to no dispatch class — and were silently dropped (2026-08-12)

`io/entsoe_hist.PSR2TECH` maps sixteen ENTSO-E labels to model technologies. `neighbours/blocks.py` then
splits those technologies into `_DISPATCHABLE` (they bid) and `_MUSTTAKE` (they set net load). **Four of
the sixteen — `waste`, `geothermal`, `other_res`, `other` — appeared in neither list**, so they were read
out of the lake, mapped to a tech, and then dropped on the floor. Nothing failed; the generation simply
ceased to exist. Measured on 2024 as a share of each zone's own load:

    NL 34.6 %   IT_NORTH 8.8 %   IT_SOUTH 8.2 %   PL_CZ 2.8 %   BE 2.7 %   DK 2.3 %
    DE_LU 1.9 % AT_SI 1.8 %      GB 1.3 %         ES 1.2 %      FR 0.4 %   PT 0.5 %

**The Netherlands is a third of its own load.** This was found while chasing NL's +23.5 EUR/MWh dispatch
error, and it is the single largest defect the model has carried. The 2024 balance closes on it exactly:

    model asks NL dispatchable for  10.91 GW   (net load 10.44 + net exports 0.47)
    observed NL thermal              4.91 GW
              + Other                4.21   + Waste 0.32   + flat load residual 1.48
                                    10.92 GW      <- closes to 0.01 GW

### NL's `Other` is Dutch solar, and `Solar` is a 0.4 GW stub

The four are not one phenomenon and must not be treated alike. Measured per zone on 2024 (`day` = 10-15
UTC mean, `night` = 22-05, `corr` = correlation with that zone's own day-ahead price):

| category | day/night | corr price | reading |
|---|---|---|---|
| `waste` / `geothermal` / `other_res`, every zone | 0.95–1.01 | ~0 | flat must-run |
| **NL `other`** | **3.04** | **−0.444** | **solar** |
| every other zone's `other` | 0.98–1.39 | +0.09 … +0.47 | price-following |

TenneT reports the decentralised Dutch fleet under `Other` and leaves `Solar` a stub reading **0.055 GW
mean / 0.399 GW peak against a fleet of ~26 GW**. `corr(Other, Solar) = +0.871`; the diurnal runs 2.0 GW at
03:00 to 7.7 GW at 13:00; the seasonal peak is April–July. A model reading `Solar` therefore saw 0.4 GW of
peak Dutch PV where ~12 GW exists — which is why NL carried 458 observed negative hours against roughly
zero modelled, and why winter evenings (solar zero, industrial floor only) ran to VoLL: 3.3 GW missing at
2024-01-17 16:00 against a measured shortfall of 0.8 GW.

This also **retires an earlier misdiagnosis**. The `participation_caps` ceiling was suspected first, and it
is not the cause: NL gas peaked at 9.55 GW in 2024 against a 9.12 GW ceiling, and observed gas in the VoLL
hour was 8.15 GW — the ceiling never bound. Relaxing it would have loosened a mechanism that is very nearly
correct, to compensate for generation that was missing somewhere else entirely.

### Decisions

**Netting is sound here, and it was checked rather than assumed** — it is exactly the trap the GB entry
above documents from the other side. If NL's load were already net of this generation, subtracting it again
would double-count. The 2024 balance says it is not: `load 13.10 = generation 12.09 + net imports (−0.47) +
residual 1.48`, with the residual **flat to ±0.03 GW across day and night** (p10 1.45, p50 1.48, p90 1.50).
A flat residual carries no solar shape, so the load series is gross. (The 1.48 GW itself is an unexplained
load-definition offset — station service, pumping, or a boundary difference. It is level, not shape, and is
left for a later pass rather than folded into this one.)

* **`waste` / `geothermal` / `other_res` → must-run in every zone, taken whole.** IT_CNOR's geothermal
  averages 599 MW against a 636 MW peak — flat to within 6 % — and waste-to-energy burns because the waste
  arrives, not because the price cleared.
* **`other` → split exactly**, `mustrun[h] = min(floor[day(h)], other[h])` and `variable = other − mustrun`,
  so the parts sum back to the metered series in every hour with no leakage either way. The floor is each
  day's night minimum smoothed by a centred 7-day rolling median. What a fleet never drops below is
  price-insensitive whatever its fuel, so the floor is must-run in **every** zone.
* **The variable part is credited to must-take RES only where the series is demonstrably solar-shaped**
  (threshold 2.0; NL scores 3.04, the runner-up 1.39 — the threshold sits in a wide gap and is not doing
  delicate work).
* **Must-run lands on load, solar lands on must-take** — not interchangeable. In projection, load scales
  with the demand factor and must-take with the RES trajectory, which are the right drivers respectively
  for waste/geothermal/industrial baseload and for Dutch PV. `weather_shapes.py` applies the same
  correction *before* its temperature regression, since `load_coef` is what `shape()` evaluates to produce
  a projected load — fitting it on gross load while reporting a net `mean_load_mw` would leave the two
  halves of that model describing different quantities.
* **GB is excluded by construction, not by exception.** `gb_embedded` nets a load-vs-generation *residual*,
  and a residual already absorbs everything not otherwise represented; applying both would subtract GB's
  `other`/`waste` floor twice.

An early version of the floor used a per-month p10 over night hours and **leaked baseload into the solar
bucket**: 0.9 GW of "solar" at midnight and a Dutch PV total of 26.3 TWh against IRENA's ~21. A floor must
sit *at* the night level, not under its tenth percentile, because solar at night is zero rather than small.
The day-tracking floor gives **22.3 TWh** — within 6 % of IRENA, on a quantity derived purely from shape.
`tests/test_unclassified_gen.py` pins that as a regression, along with the exactness of the split.

### Left out deliberately

The **variable** part of `other` where it is price-FOLLOWING: IT_NORTH 12.0 TWh at corr +0.330, the Italian
south, GB 3.3 TWh at +0.465 — 8.6 TWh unrepresented in total, 7 % of the 131.2 TWh at stake. That
generation is real and it is dispatchable, so representing it needs a short-run marginal cost, and `other`
is a residual label with no fuel. Netting it off load would assert it is must-run, which the positive price
correlation refutes; a guessed SRMC would be an invented supply curve in the two zones whose levels are
already the worst in the model. It stays out until it can be given a cost. `scripts/audit_unclassified.py`
prints the split per zone so the omission is found by measurement rather than rediscovered as a surprise.

### Measured — the solar half shipped, the must-run half did not

The two halves were gated separately (`DISPATCH_UNCLASSIFIED_SOLAR` / `DISPATCH_UNCLASSIFIED_MUSTRUN`)
once the first combined measurement came back worse, because they are independent corrections that only
happened to arrive together. Multi-year gate:

| arm | \|mean err\| | log_err | NL 2024 neg (obs 458) |
|---|---|---|---|
| pre-fix baseline | 11.15 | 0.737 | 951 |
| **solar half only — SHIPPED** | **11.00** | **0.692** | **379** |
| both halves | 12.71 | 0.785 | 742 |
| both, solar netted off load | 13.88 | 0.849 | 1829 |

The **solar half** beats the baseline on both pooled metrics and leaves every other zone within
0.5 €/MWh (FR −4.3→−4.2, DE_LU −23.9→−23.8, CH +7.3→+7.2) — it is a NL-only correction that *replaces*
a synthetic reconstruction rather than adding supply. On by default.

The **must-run half** is off by default. It costs 1.7 €/MWh of pooled mean error because it lowers every
zone ~4 €/MWh — helping the three zones priced too dear and hurting the five already priced too cheap.
CH had **0.00 GW added yet moved −5.7**, so most of the movement is contagion, not local supply. The
generation is real; this is not a correctness verdict. It is that adding correct supply to a model with a
pre-existing cheap bias moves it further from observation, and the bias must be found first.

### It supersedes `btm_solar` — the model was already reconstructing this fleet, wrongly

The decisive finding, and the reason the first combined measurement looked like "correct but harmful".
`rolling/backtest.py` and `rolling/projection.py` already reconstructed Dutch behind-the-meter PV via
`flexibility/res_potential.btm_solar`, on the stated premise that the fleet is *"invisible on BOTH sides
of the ENTSO-E balance — not in generation (behind-the-meter)"*. **The premise is false.** The
"0.5 TWh/yr metered" it cites is the `Solar` key alone (0.055 GW × 8784 h = 0.49 TWh); the fleet is in
`Other`, which no dispatch class claimed — dropped, not absent.

|  | energy | peak | peak/nameplate |
|---|---|---|---|
| `btm_solar` | 28.4 TWh | **22.28 GW** | **0.80** |
| metered `Other` solar | 22.3 TWh | 13.04 GW | 0.47 |
| actual (IRENA/CBS) | ~21–23 TWh | | |

`corr(btm, metered) = +0.913` — the same fleet twice. The **energies nearly agree; the peak is what was
wrong**, and a 0.80 peak factor across 28 GW of mixed roof pitch and orientation is unreachable. That
excess peak was NL's phantom midday surplus: **727 model-only negative hours in 2024, every one between
05:00 and 15:00 UTC, March–September**, with NL the cheapest zone in nearly all of them. Running both
paths gives 50.6 TWh at a 35.3 GW peak against a real ~21 TWh — the double-count that produced NL's
−27.4 €/MWh in the first arm. Both call sites are gated on `solar_enabled()`, so disabling the half
restores the synthetic path rather than leaving NL with no solar.

Corroborating: observed NL and DE_LU go negative together in **86 %** of NL's negative hours (396/458);
the pre-fix model managed **24 %** (231/951). It had decoupled the Netherlands from Germany.

### Netting it off load instead — TRIED, MEASURED, REJECTED

Net load is identical whether the solar part is subtracted from `load_mw` or added to `musttake_res_mw`
(`load − mustrun − solar − musttake` regrouped), so the choice looks cosmetic. It is not: it decides
whether the solar is **curtailable**. Netted off load it becomes inflexible negative demand, so NL's
surplus hours ran past the RES bid floor to the **−500 EUR/MWh price floor in 997 hours of 2024**, and NL's
pooled mean error went −19.8 → **−60.6**. Every other zone came back bit-identical, which is the tell: the
thermal dispatch never moved, only NL's own dual.

This **refines the GB decision above rather than contradicting it**. `gb_embedded` nets embedded generation
off demand precisely to avoid manufacturing negative prices — and that manoeuvre is only safe for genuinely
inflexible generation. Applied to a 13 GW-peak solar fleet in a zone that does price negative, it does not
suppress the low tail, it detonates it. The distinction to carry forward is flexibility, not accounting.

## Published NTC "throttling" — CLAIMED, TESTED, RETRACTED; the truth is the opposite (2026-08-13)

Recorded because the wrong version of this was written into this file for several hours, and because the
error is an easy one to repeat.

**The claim.** Chasing NL's residual surplus, the applied NTC was compared against `entsoe_flows` and
found to be exceeded constantly — NL→DE_LU capped near 1081 MW against flows reaching 5171 MW, over the
cap in 32 % of 2024 hours; FR→CH, DE_LU→CH, FR→IT_NORTH and BE→NL the same. The reading was that the
model forbids transfers that physically happened, isolating NL and widening both its price tails.

**Why it is wrong, twice over.**

1. `entsoe_flows` is ingested via `client.query_crossborder_flows` — ENTSO-E **physical flows** (A11), not
   scheduled commercial exchanges (A09). In a meshed AC grid physical flow routinely exceeds commercial
   NTC through loop flows and transit, so "flow > NTC" is ordinary grid physics and proves nothing. The
   first comparison was also made against the series MEDIAN, which is not even the right scalar: the
   published series is genuinely hourly (NL→DE_LU has 751 distinct values spanning 0–4907 MW).
2. The valid test is price coupling — when two zones clear at the same price the border was not congested,
   so the commercial exchange was unconstrained in that hour. In those hours the published NTC is **not**
   small: NL→DE_LU median 1081 MW with only 130 of 1754 coupled hours below 500 MW. The published series
   is consistent with observed coupling.

**And the model runs the other way.** Comparing model against observed price convergence (|ΔP| < 1 €/MWh)
shows the model **UNDER**-congests nearly every border — its zones are too tightly coupled, not too
isolated:

| border (2024) | observed coupled | model coupled | gap |
|---|---|---|---|
| CH→IT_NORTH | 3.3 % | **69.1 %** | +65.8 |
| BE→FR | 30.1 % | 74.0 % | +43.9 |
| CH→DE_LU | 8.2 % | 50.1 % | +41.9 |
| FR→CH | 5.6 % | 25.2 % | +19.6 |
| NL→DE_LU | 41.3 % | 49.4 % | +8.1 |

2025 is worse on every line. Over-coupling compresses the zonal price distribution — it drags the dearest
zone down and lifts the cheapest — which is the shape of IT-North's **−12.7 €/MWh** (observed the dearest
zone at 107) sitting coupled to Switzerland 69 % of the time against 3.3 % observed. The model reproduces
only half of the observed IT-North↔CH spread (16 €/MWh against 31).

**Not yet a cause, and deliberately not written up as one.** Price-equality frequency conflates two
mechanisms: an over-generous border AND two zones simply sharing a marginal fuel. Both would show as
coupling. A plausible contributor is that `flow_derived_ntc` takes p99.5 of realized PHYSICAL flow and
uses it as a COMMERCIAL transfer limit — the same physical-vs-commercial confusion that produced the
retracted claim above, but this time in the model rather than in the analysis, and biased toward
capacities that are too large. Discriminating between that and a stack/fuel explanation needs the model's
own border flows, which the gate does not currently persist. **Open, and the largest lead still standing.**

## `solar_uplift` booked cloud cover as curtailment — FIXED (2026-08-13)

A parallel audit of every synthetic reconstruction in the model (5 investigations, adversarially verified)
found `flexibility/res_potential.solar_uplift` — applied to EVERY neighbour zone at `backtest.py:221` —
adds **46.1 TWh (2024) and 51.1 TWh (2025)** of zero-cost must-take RES across the neighbour zones, of
which **70–72 % lands on hours the market priced ≥50 €/MWh**, where no operator curtails PV.

The falsification is clean: the estimator returns the **same 16–25 % uplift in zone-years where
curtailment was impossible because the zone never priced negative**. IT_NORTH has zero negative hours in
2019, 2022, 2024 *and* 2025, yet is uplifted 16.3–24.6 % every year. A curtailment-free synthetic placebo
reproduces 102–150 % of the real uplift in all 14 zone-years tested — i.e. essentially all of it is
day-to-day cloud variability, not censored potential. A defensible upper bound on the genuine signal
(the negative-hour dip in excess of the hour-of-day-matched positive-hour mean) is DE_LU 0.99 TWh,
ES 0.65, GB 0.07, IT_NORTH 0.00 — roughly a tenth of what DE_LU alone is booked.

Two scope facts that bound the damage: it is **peak-bounded** (uplifted peak equals observed peak in all
28 zone-years tested, so unlike `btm_solar` it cannot inflate the peak), and it **does not reach the
forward projection** (`projection.py` imports only `btm_solar`). It is an energy/placement bias in the
backtest and gate-scoring path — 82.4 TWh across the four gate years.

Separately, its price filter is **entirely disabled** for DK, PL_CZ, AT_SI and IT_SOUTH: `_observed_prices`
looks up the virtual-zone key while the lake stores prices under the constituent keys, so `prices=None`
and the noise floor falls to 0 — the docstring's "conservative over-uplift, flagged for projection use"
branch firing silently in the backtest for 4 of 12 zones, worth +12.05 TWh in 2025. Same "dropped rather
than absent" template as the Dutch `Other` defect.

**This is the leading candidate for the cheap bias** that made the must-run half unshippable: ~46 TWh/yr
of free zero-cost energy in the neighbour zones is exactly what would hold DE_LU at −23.9 and BE at −11.6.

### The fix: a floor has to be in the tail, not at the centre

**Root cause, in one line: the noise floor was a MEDIAN.** The term exists to subtract ordinary cloud
variability so that what survives is censored potential — but taking its *centre* leaves half of every
uncensored hour's dip above it by construction. Two changes, both in `flexibility/res_potential.py`:

1. `_NOISE_Q = 0.90` — the floor is now an upper quantile of the same-hour dip over clearly-uncensored
   hours. Measured effect on the 2024 inputs, summed over the 12 neighbour zones:

       floor quantile   0.50 (was)   0.75    0.90 (now)   0.95   0.99
       uplift TWh             29.0   11.6           3.6    1.5    0.2

2. `prices=None` ⇒ **no uplift**, replacing `floor = 0` (the full dip counted as curtailment — maximum
   uplift precisely where there is least evidence, and described in the docstring as "conservative"). The
   call site in `backtest.py` now averages CONSTITUENT prices for the aggregate zones, so IT_SOUTH gets a
   properly floored uplift instead of none; DK/PL_CZ/AT_SI have no constituent prices in the lake for
   2024 and now correctly receive nothing rather than an unfloored ~17 TWh.

Together: **46.1 TWh → 3.6 TWh** on 2024.

`q90` is calibrated on two independent axes. On real data it lands nearest the audit's bound on the
genuine signal (DE_LU 1.29 vs 0.99 TWh, ES 0.77 vs 0.65, GB 0.26 vs 0.07; q95 undershoots DE_LU and ES by
about half). On a synthetic pair of solar years — one curtailment-free, one with curtailment injected on
negative-price hours — it trades recovery for precision the right way:

| floor q | placebo leak | real curtailment recovered | **false uplift on clean hours** |
|---|---|---|---|
| 0.50 (was) | 15.8 % | 90.0 % | **191 %** |
| 0.90 (now) | 0.7 % | 25.2 % | **9.9 %** |
| 0.95 | 0.3 % | 15.2 % | 1.4 % |

At the old setting the estimator invented nearly twice as much curtailment on clean hours as it recovered
on real ones — and since most hours are uncensored, that leak *is* the 46 TWh. Under-recovering real
curtailment is the safe direction; inventing it is not. `tests/test_res_potential.py` pins the placebo
property and both thresholds at their measured values.

## Le derating nucleaire global plafonnait quatre parcs sous leur propre moyenne observee (2026-08-13)

`_AVAIL_FACTOR["nuclear"] = 0.78` is a single global constant. On 2024 it put the capacity ceiling of four
zones **below their own observed annual mean output**:

| zone | nameplate | ×0.78 | observed **mean** | observed max |
|---|---|---|---|---|
| BE | 3929 | 3065 | **3385** | 3971 |
| ES | 7117 | 5551 | **5957** | 7118 |
| CH | 2970 | 2317 | **2614** | 3036 |
| NL | 486 | 379 | **385** | 490 |

A ceiling below the mean of what actually happened is not a modelling choice — no dispatch can reproduce
the observed year against it. ~1.0 GW of baseload was permanently absent.

### A floor, never a replacement — and that is the whole safety argument

`METHODOLOGY.md` records why this module moved AWAY from generation quantiles to nameplate × derating: a
quantile UNDER-reads plant that rarely runs at full output (DE gas read 11.8 GW against 31.7 GW installed,
which over-priced Germany). `_measured_avail` therefore returns `clip(median(observed)/nameplate, default,
1.0)` — it can only ever **raise** a factor, so the old failure mode is unreachable. Verified on 2024
across every zone and technology:

* **binds** — BE/CH/ES/NL nuclear, whose median output is 0.85–1.00 of nameplate;
* **no-op** — every peaker, by a wide margin: DE_LU gas p99.5 = 0.48 against its 0.90 default, ES coal
  0.29 against 0.88, DE_LU oil 0.20 against 0.85;
* **no-op** — GB nuclear, whose AGR fleet genuinely runs at a median of 0.50 and correctly keeps 0.78.

**The MEDIAN, not a high quantile.** p75/p95 put these fleets at ~1.00 of nameplate, which would let the
model run them flat out in every hour and over-supply the mean instead of under-supplying it. The median
carries the honest claim — *the derating may not assert less capacity than the fleet delivered in half its
hours* — and clears the defect with the least overshoot (BE 0.88 × 3929 = 3456 against a mean of 3385).
Result: BE 3065→3456, CH 2317→2922, ES 5551→6070, NL 379→478.

### Measured: best pooled mean error and best scarcity recall of any arm

| arm | \|mean err\| | log_err | negatives (obs 6136) | >200 €/MWh (obs 34917) |
|---|---|---|---|---|
| pre-`unclassified` baseline | 11.15 | 0.737 | 4915 | 36418 |
| + NL metered solar + `solar_uplift` q90 | 11.08 | **0.652** | 2523 | 37091 |
| **+ per-zone availability floor** | **10.45** | 0.666 | 2912 | **35533** |

Both modern years improve on BOTH metrics (2024 |mean| 10.13→8.31 and log_err 0.838→0.820; 2025 8.78→8.17
and 0.797→0.794). Scarcity recall lands 616 hours off target against the previous arm's 2174.

**The split is informative, and it is the same split as everything else this session.** The three zones
that improve are the over-priced ones, and they improve a lot — **CH +10.1 → +4.8**, PT +8.3 → +3.9,
NL +4.2 → +3.1. The five that worsen were already priced too cheap and move ~2 €/MWh each (BE −8.6 →
−11.7, DE_LU −19.8 → −21.8, FR −2.0 → −4.4, ES +1.7 → −3.0, IT_NORTH −4.3 → −6.3). Adding physically
real capacity to a model with a residual cheap bias will keep doing this: **DE_LU at −21.8 and the 2022
merit order remain the dominant unexplained terms**, and the single log_err regression here is confined to
2022 (0.628 → 0.713), the year whose coal-ahead-of-gas defect is diagnosed and unfixed.

On by default; opt out with `DISPATCH_ZONE_AVAIL=0`. The flag is part of the `_STACK_CACHE` key so an
in-process A/B cannot be served the other arm's stack. `tests/test_zone_availability.py` pins the floor
property, the peaker no-op, the 1.0 cap and the fallbacks.

**Time-varying neighbour availability is the structurally right answer** — a flat number cannot represent
a fleet that is either at nameplate or in outage. This only stops the flat number from being provably too
low.

## Le lignite de fond de mine etait facture au prix du charbon maritime (2026-08-13)

`stacks/costs.py` mapped `"lignite" -> "coal"` in `FUEL_COMMODITY`, so German/Polish/Czech lignite was
charged the seaborne hard-coal index. Lignite is dug in an opencast pit beside the plant, moved by
conveyor, never traded and never shipped; its cost is a **production** cost of ~1.2–1.8 €/GJ ≈ 4.3–6.5
€/MWh_th, essentially flat year to year. In July 2022 the model charged it **37.9 €/MWh_th — about
264 €/t of hard coal — for fuel that costs ~5**.

**This repo recorded the symptom without recognising it.** The Phase-3 verification note above reads:
*"2022 gas €340 » coal €147 / lignite €159"*. Lignite above hard coal is an **inversion** — lignite has
the worse efficiency and the higher CO2 intensity but by far the cheaper fuel, which is precisely why
German lignite runs baseload and hard coal does not. Corrected SRMC for July 2022, worst-efficiency block
of each class: **lignite 195 → 101, coal 184, gas 473**.

### It makes the backtest worse, and it ships anyway

| arm | \|mean err\| | log_err | DE_LU 2022 mean err |
|---|---|---|---|
| previous shipped | **10.45** | 0.666 | −79.7 |
| + lignite fuel corrected | 11.35 | 0.667 | **−95.3** |

The over-priced lignite was a **compensating error**: it propped up the German price plateau and masked
part of a deficiency that lives elsewhere. Removing it exposes the true size of the DE_LU 2022 gap.

Shipped ON regardless, for three reasons:

1. **`log_err` is unchanged (0.666 → 0.667).** The distribution's *shape* is untouched; only the level in
   one year moved. 2024 improves marginally on both metrics.
2. **Projection integrity, which is the decisive argument.** This is a 2027-46 projection model. Chaining
   lignite to a seaborne index means every scenario in which coal prices rise makes German lignite look
   uneconomic and retires it from the merit order — physically false, and it would distort twenty years
   of projected German baseload. The compensating benefit is backtest-only; the damage is structural and
   forward-looking.
3. Keeping a known-false input because it happens to offset another error is how a model becomes
   unfalsifiable. The offset is not robust — it depends on the coal price staying high.

`DISPATCH_LIGNITE_FUEL=0` restores the old behaviour for A/B or if backtest level is preferred.

### What this says about the 2022 merit order — the request that produced it

The chantier was "fix the 2022 merit order". Two hypotheses were tested and **both failed**:

* **Rhine low water restricting coal barges — REFUTED on three independent counts.** The Kaub gauge
  bottomed in mid-August yet DE coal's monthly max ROSE 8552 (Jun) → 8977 (Jul) → 11381 (Aug, +33 %) →
  12743 (Sep, +42 %), straight through the worst low water; strictly tidewater plants immune to Rhine
  levels show a LARGER relative summer outage swing (19.7×) than Rhine-fed ones (3.8×); and the entire
  barge-fed fleet above the bottleneck is 3.9 GW nominal (5.5 GW including the Saar/Moselle), too small
  to open an 8 GW hole. Monthly coal max instead tracks the REMIT outage calendar at corr = −0.839.
* **"It is just summer maintenance seasonality, and 2022 is unremarkable" — ALSO REFUTED**, and this is
  the subtler error. Comparing summer dips across years has **no valid control**: coal SRMC vs spot in
  May–Jul was in-merit 15 % of hours in 2019, 12 % in 2023, 24 % in 2024, 27 % in 2025 — but **79.7 % in
  2022**, with 177-hour continuous in-merit runs against 7–12 hours elsewhere. In every other year the
  output envelope measures *demand for the fleet*, not its availability. 2022 is the ONE year in which
  "did not run" implies "could not run", so it is the one year the seasonality argument cannot be applied
  to.

**The 8 GW of summer coal headroom is real and it is availability** — but the honest sizing rules it out
as the fix: removing 8 GW in May–Jul is worth **+6.45 €/MWh, 8 % of DE_LU 2022's −79.7 bias**. Capping
coal at its metered monthly max in every month reaches only +18.6. Deleting the ENTIRE German hard-coal
fleet all year is the absolute ceiling at +73.0, and it breaks October and November. The reason is that
**lignite is interleaved with coal in the merit order**: removing a coal block hands the margin to a
lignite block €13–20 away, never to gas. For gas to set the German price at the median, ~13.2 GW must
leave the 31.7 GW coal+lignite stack in Jun–Aug — twice what any coal-availability story can supply.

**Left alone, deliberately: the coal index.** `commodities/public_sources.py` already documents that API2
is not published in the World Bank sheet and that "Coal, South African" (FOB Richards Bay) is a proxy with
its own basis to ARA. Measured, it runs **17–18 % low** in 2022 — 32.78 €/MWh_th against 39.3 (CIF NWE
marker) / 40.0 (German border price). That is a real gap, but closing it needs an ingested ARA series, not
an invented basis adjustment, and it is worth ~5 % of the summer mis-pricing.

**So the 2022 merit order is NOT fixed.** What is fixed is one unambiguous input defect found while
looking at it. The remaining German gap is a stack-size/level problem that no coal fix reaches, and it is
now the largest single unexplained term in the model.
