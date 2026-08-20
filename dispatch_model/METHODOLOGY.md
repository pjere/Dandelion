# dispatch_model (step vi) — methodology note

Multi-zone economic dispatch producing **hourly zonal system marginal prices** for a 13-zone European
footprint (FR unit-resolved; every other zone aggregated, GB included since it was promoted from a border
curve to a balance of its own on Elexon/BMRS data). Consumes steps
(ii)–(v) + a commodity module. Its output is **system marginal cost (SMC)**; **step (vii)** applies a
calibrated markup to map SMC → observed day-ahead spot.

## LP formulation

Continuous linear dispatch (no unit commitment — see simplifications), price = dual of energy balance.
Per zone `z`, hour `t`:

    min  Σ_z Σ_t [ Σ_u srmc_u·gen_{u,t} + res_bid·res_{z,t} + VoLL·ens_{z,t} + floor_cost·dump_{z,t} ]
         + ε Σ_k Σ_t (fwd_{k,t} + bwd_{k,t})
    s.t. Σ_{u∈z} gen_{u,t} + res_{z,t} + ens_{z,t} − dump_{z,t} + imports_{z,t} = demand_{z,t}   [price_{z,t}]
         imports_{z,t} = Σ_{(a,z)} (fwd−bwd) − Σ_{(z,b)} (fwd−bwd)
         gmin_{u,t} ≤ gen_{u,t} ≤ availability_{u,t}·capacity_u        (nuclear modulation floor)
         0 ≤ res ≤ res_pot ;  0 ≤ fwd_k ≤ NTC_ab ,  0 ≤ bwd_k ≤ NTC_ba
         Σ_{u∈z,tech=hydro_res, t∈window} gen ≤ weekly_reservoir_budget_z    [water_value_z]

`srmc = fuel/η + CO2_int/η·EUA + VOM` (nuclear ≈ €7 flat; hydro-reservoir bid ~0, energy-limited).
**Lignite is priced at its mine-mouth production cost (~5 €/MWh_th), not off the seaborne coal index** —
it is dug beside the plant and never traded. Charging it the hard-coal price inverted the German merit
order (July 2022: lignite €195 *above* coal €184, for fuel costing ~5 against the 37.9 €/MWh_th it was
billed); it now reads lignite 101 < coal 184 < gas 473. See `DECISIONS.md` — the correction makes the
2022 backtest level *worse* by removing a compensating error, and ships anyway because chaining lignite
to a seaborne index would distort twenty years of projected German baseload.
Scarcity is priced inside the LP — DSR tranches (300/1000/4000 €/MWh as high-SRMC "units"), unserved
energy at VoLL, RES curtailment at `res_bid<0`, over-generation `dump` at the price floor — so negative
and scarcity prices, and cross-border spreads, are **duals, never post-processed**.

## FLEX module — plant operating rigidities & the endogenous negative tail (opt-in, 2026-07)

Behind `flexibility.enabled` (default off, flag-off byte-identical), the LP above gains per-FR-reactor
rigidity rows — commitment `u ≥ κ·avail` (the campaign is *scheduled*; the LP cannot shed the fleet), a
two-tier operating band (free modulation to `alpha_band_op`=0.74 — the revealed socle share; deep-mod `d`
below it at `c_mod`=45, the revealed socle bid), 8h/daily deep-mod budgets, a xénon-limited up-ramp,
start/min-down costs, end-of-cycle maneuverability from REMIT, reserves, a grid-stability floor,
window-seam state, and the OA/CR/merchant downward bid ladder with vintage decay. Still a pure LP: every
price remains a balance dual. Calibrated on 2024: **335 modelled vs 352 observed FR negative hours**;
depth beyond the merchant rung requires neighbour-zone surplus fidelity (backlog). Full algebra, phase
history, and the calibration report: `FLEXIBILITY.md` and `FLEX_CALIBRATION_2024.md`.

## Hydro decomposition

Two-level, **option (b)**: weekly reservoir energy budgets from historical seasonal generation (guide
curves), scaled by wetness; the hourly LP self-allocates the budget to peak hours and the **water value =
dual of the budget cap**. Interface preserved for an SDDP water-value swap (option a). PSP storage
arbitrage and coarse neighbour-hydro budgets are refinements.

## Simplifications (and expected price-impact sign)

- **LP, not MILP** (no start-ups/min-up-down) → slightly *understates* peak/spike prices (no start-up
  recovery) — handled by step (vii).
- **NTC coupling** (flat, representative) not flow-based → *over-couples* zones, *compressing* spreads and
  *suppressing* zonal negative-price events. Real time-varying NTCs (`rte_ntc`/ENTSO-E) restore spreads.
- **Neighbour capacity** = ENTSO-E installed capacity × an availability derating (`_AVAIL_FACTOR`), with
  a p99.9-of-generation fallback only where nameplate is missing. The derating is **floored per zone at
  the fleet's own median output** (`_measured_avail`): the global `nuclear = 0.78` put BE/ES/CH/NL's
  ceiling *below their observed annual mean* — 3065 MW against a 3385 MW mean in Belgium — which no
  dispatch can reproduce, so ~1.0 GW of baseload was permanently absent. The floor only ever RAISES a
  factor, so it cannot reproduce the failure that made this module abandon generation quantiles in the
  first place (DE gas read 11.8 GW against 31.7 installed, over-pricing Germany): measured on 2024 it
  binds on BE/CH/ES/NL nuclear alone and is a no-op for every peaker (DE_LU gas p99.5 = 0.48 vs its 0.90
  default) and for GB's genuinely-derated AGR fleet. Gate effect: pooled |mean error| 11.08 → **10.45**,
  scarcity recall 616 hours off target against 2174, both modern years better on both metrics — carried
  by the over-priced zones (CH +10.1 → **+4.8**, PT +8.3 → +3.9) at the cost of the already-too-cheap ones
  (BE −8.6 → −11.7). A flat number still cannot represent a fleet that is either at nameplate or in
  outage; time-varying neighbour availability remains the structurally right answer.
- **Projected interconnection**: `dispatch_ntc_newbuild` carries one row per interconnector project with
  its commissioning year, summed as a **step** (never interpolated) and applied as an MW **delta** on the
  reference-year hourly NTC — an interconnector is a discrete asset like a reactor, not a smooth
  trajectory. Two tiers are on by default (`built`, `reference`); TYNDP's assessed candidates are written
  but off, since enabling them asserts every project under CBA gets built. Before this the grid was the
  one input frozen at reference-year level while demand and generation evolved, which biased projections
  toward too much congestion. See `TYNDP_SOURCES.md`.
- **Backtest availability**: FR nuclear from rolling-max output; REMIT (task #41) is the ground-truth
  upgrade. GB is now a modelled zone on Elexon data (its old fixed border curve is gone), but ~2.8 GW of
  its interconnection — NSL to Norway, Moyle/EWIC/Greenlink to Ireland — reaches zones the model does not
  carry; measured on 2024 the two nearly cancel at +505 MW net, 1.8 % of GB demand.
- **GB embedded generation** (`io/gb_embedded.py`): GB is the one zone whose load and generation come from
  feeds measured on *different boundaries* — ITSDO is demand at the transmission boundary, already net of
  distribution-connected plant, while FUELINST/AGWS meter transmission-connected plant only. The two
  therefore do not balance (5851 MW short on 2024, a fifth of demand), where every ENTSO-E zone balances by
  construction. The residual is measured hour by hour and netted off GB demand as a month × hour-of-day
  median. It decomposes, by measurement, into an exact solar double-count (AGWS reports national solar,
  which GB metering has already removed from ITSDO — regression coefficient −1.25, and corr(residual +
  solar, solar) = −0.065) and a flat ~7.3 GW embedded firm block. Netting it off demand rather than adding
  it as supply keeps it out of price formation, which is right for heat-led CHP and conservative for the
  rest. Consequence: GB's own stack adequacy is no longer tested — acceptable because GB exists here to be
  a correct neighbour for FR/BE/NL.
- **Unclassified generation** (`io/unclassified_gen.py`): `PSR2TECH` maps sixteen ENTSO-E labels, but
  `_DISPATCHABLE` and `_MUSTTAKE` between them claimed only twelve — `waste`, `geothermal`, `other_res`
  and `other` were read from the lake and then dropped, silently. On 2024 that is **34.6 % of Dutch load**,
  8.8 % of IT-North's and 8.2 % of the Italian south's. The Netherlands is the severe case because TenneT
  reports the decentralised solar fleet under `Other` and leaves `Solar` a stub (0.055 GW mean / 0.399 GW
  peak against ~28 GW installed): NL `Other` has a day/night ratio of 3.04 and correlates **−0.444** with
  price and **+0.871** with NL's own `Solar` series, where no other zone's `Other` exceeds 1.39.
  `other` is split exactly into a day-tracking night floor and a remainder credited to must-take **only
  where the shape is solar**; the recovered Dutch PV is 22.3 TWh against IRENA/CBS ~21–23.
  **This SUPERSEDES `flexibility.res_potential.btm_solar`**, which reconstructed the same fleet from
  nameplate on the false premise that it was invisible in generation: 28.4 TWh at a **22.28 GW peak
  (0.80 of nameplate)** where the metered fleet peaks at 13.04 GW (0.47). The energies nearly agree — the
  peak was the error, and it produced 727 phantom NL negative hours in 2024, all 05–15 UTC, Mar–Sep.
  Shipped: **solar half ON, must-run half OFF** (|mean err| 11.15 → 11.00, log_err 0.737 → 0.692, NL 2024
  negatives 951 → 379 against 458 observed). The cross-zonal must-run half stays off — it is real
  generation but lowers every zone ~4 €/MWh into a pre-existing cheap bias; see `DECISIONS.md`.
- **Known-open** (`DECISIONS.md`): the model UNDER-congests most borders — CH-IT_NORTH clears at an equal
  price in 69 % of 2024 hours against 3.3 % observed, BE-FR 74 % against 30 % — which compresses the zonal
  price distribution and is the leading suspect for IT-North's -12.7 EUR/MWh. A candidate mechanism is
  that `flow_derived_ntc` reads p99.5 of realized PHYSICAL flow (loop flows included) as a COMMERCIAL
  transfer limit. An earlier claim in the opposite direction — that published NTCs throttle borders below
  realized flow — was tested and RETRACTED; see `DECISIONS.md`.
- **Seasonal neighbour thermal availability** (`blocks.monthly_avail`, backtest only): `participation_caps`
  clamps to the revealed fleet with ONE number per year, so the model could spend a November availability
  in June. Measured on German coal+lignite, **7-12 GW never delivers even at the 100 dearest hours of the
  year, in every year** — chronic, but only visible in the price when the gas-coal spread is large (€1 in
  2019, €14 in 2024, **€184 in 2022**). A month is used only where the tech was inframarginal for >=50 %
  of its hours, so low output cannot be misread as unavailability in cheap years; gas never qualifies,
  correctly, being the marginal unit. Gate effect: pooled |mean err| 11.35 -> **9.96**, log_err 0.667 ->
  **0.654**, DE_LU 2022 -95.3 -> **-63.5** with its >200 EUR/MWh hours 1487 -> 2998 against 4642 observed,
  and flat log_err in 2019/2024/2025 where the test declines to act.
- **Feed-coverage repair** (`io/ch_hydro.py`): ENTSO-E series are not always complete, and the model had no
  notion of it. Swiss run-of-river reads 1.95–2.27 TWh in 2019–2024 and **14.50 TWh in 2025** — new filers
  plus a reclassification out of reservoir (13.9 → 9.9 TWh) — with declared capacity flat across the step
  and a pre-2025 declaration that refutes itself (0.63 GW nameplate, 0.98 GW metered peak). ROR is
  must-take and peaks on snowmelt, which is when Swiss prices go negative, so five sixths of the fleet was
  missing exactly when it mattered: **15 modelled negative hours against 613 observed**, with a modelled
  median of +22.35 €/MWh in the hours Switzerland actually cleared negative. Repaired by scaling the
  metered series to the complete year (k = 7.07, capped at its p99 of 3.39 GW); the partial filers are
  shape-representative — normalised monthly profiles correlate 0.81–0.90 with the complete year. Added to
  must-take, **not** netted off load — the opposite of `gb_embedded`, because Swiss ROR is what spills in a
  Swiss surplus, so netting it would create the surplus and then hide it. Gate: CH mean error +5.4 →
  **−3.3** (2019 +5.8→+2.5, 2024 +13.3→+6.0, 2025 untouched), log_err 0.654 → 0.650, scarcity recall
  38225 → 36956 against 34917; cost 0.54 €/MWh of pooled mean error, nearly all of it CH 2022 overshooting
  to −16.8. A sweep of all 51 candidate coverage steps across every zone and technology found this to be
  the only large one in a scored zone — see `DECISIONS.md` for the adjudication and for the RES zero-bid
  sink, which is measured and held OFF pending a depth fix.
- **Italian nameplate** (`io/area_capacity.py`, opt-in, currently OFF): ENTSO-E publishes installed
  capacity for Italy at CONTROL-AREA level only — every bidding-zone request returns `nodata` — so the
  Italian stack runs on the p99.9-of-generation fallback, which under-reads energy-limited plant worst
  (IT-North reservoir 1.54 GW against an allocated 4.18, PSP 2.52 against 5.18). The control-area series is
  now ingested (161 rows, 2018-2026); allocating it to bidding zones by generation share validates
  geographically but measured WORSE on the gate (|mean err| 10.50 -> 12.01), improving 2024/2025 and
  collapsing 2019/2022 — see `DECISIONS.md`. GB publishes none at any level (Elexon is its source) and CH
  publishes only four technologies, which is why Swiss solar/wind are absent.
- Static reserve margin (no reserve co-optimisation); no intra-zonal grid; no strategic bidding.

## Backtest — full-year 2019 (annual baseload = §8 acceptance)

Neighbour stacks sized from **ENTSO-E installed capacity × availability derating** (not the p99.9-of-
generation proxy — that undersized peakers, e.g. DE gas 11.8→31.7 GW, and over-priced DE):

NTC per border/direction = **p99.5 of realized physical flow** (`flow_derived_ntc`) — the effective,
congestion-reflecting capability (e.g. FR→IT 3.1 GW, not the 4.35 GW nominal; DE→BE only 0.17 GW):

| zone | model €/MWh | observed | baseload err | corr |
|---|---|---|---|---|
| FR | 38.0 | 39.5 | **−3.8 %** | 0.74 |
| DE-LU | 39.4 | 37.7 | **+4.5 %** | 0.71 |
| BE | 38.0 | 39.3 | **−3.5 %** | 0.60 |
| CH | 40.8 | 40.9 | **−0.3 %** | 0.72 |
| IT-North | 41.3 | 51.3 | −19.4 % | 0.57 |
| ES | 38.4 | 47.7 | −19.4 % | 0.50 |

**4/6 zones within ±4.5 % baseload**; correlations **0.71–0.74** for FR/DE/CH (approaching the ≥0.8 gate,
which targets the step-vii-calibrated output); P50 errors ≈0. **IT-North and ES stay ~−19 % under, and this
is NOT interconnection** (flow-derived NTCs left it unchanged): they burn gas priced above TTF (Italian
PSV / Spanish MIBGAS hubs, +€2–5/MWh_th) and carry a larger day-ahead scarcity/capacity premium →
structural supply cost + step-vii markup. P95 errors are negative everywhere (SMC lacks start-up/scarcity
spikes → step vii). Single-zone FR backtest gives hourly corr 0.71.

## Contract with step (vii)

This model outputs **system marginal prices**. Step (vii) fits a calibrated markup/spread layer
(regression/ML on backtest residuals — uplift, ramping, start-up recovery, bidding behaviour, FB-coupling)
mapping SMC → day-ahead spot. The backtest residuals here (parallel level gaps, spread compression, spike
under-prediction) are precisely its training signal.

## Reading volumes out of the LP (`DISPATCH_CAPTURE_DISPATCH`, opt-in, 2026-08)

Everything this model was scored on until now is a **price**: the gate, the backtest and the golden harness
all compare price series, so the solver only ever returned duals, flows and water values. Generation stayed
inside HiGHS as an unread block of the primal vector.

A price alone cannot answer what a projection is usually asked for. Revenue is `Σₕ MWₕ × priceₕ`, and the
gap between that and `annual MWh × annual mean price` **is** the capture-rate effect — for RES it is the
whole economic question. So `lp/highs_solver._read_dispatch` now reads the blocks that were already solved
and aggregates them per `(zone, tech)`: the generation block by its unit tech labels, the RES tranches
summed to the dispatched (i.e. non-curtailed) potential, plus ENS, dump and the storage primal.

It is read-only — no column bound, no row, no cost changes — and gated off by default, because it is dead
weight for every price-scored caller. `rolling/projection.project_year` takes a matching `sink` dict which
retains the year's dispatch, flows and both price series; left `None` the memory profile is unchanged.

The property that makes the capture checkable rather than merely plausible is the zonal balance:

```
gen + res + ens − dump + storage_discharge − storage_charge + net_import = demand
```

Block extraction is index arithmetic on a flat primal vector, where an off-by-one in a base offset or a
unit-major/time-major reshape confusion yields numbers of the right magnitude and sign attributed to the
wrong plant — plausible, and wrong. `tests/test_dispatch_capture.py` asserts the identity above against the
LP's own balance rows, which is the check such a mistake cannot pass.

`scripts/run_projection_20y.py` drives the horizon with this on and persists parquet per year;
`scripts/build_projection_deliverables.py` turns that into revenue, congestion-rent and negative-hour
tables. Congestion rent is `Σₕ flowₕ × (p_importing − p_exporting)`, which on SMC is non-negative by
construction — a useful self-check, since flow only ever runs cheap→dear in the LP. On post-markup spot it
can go negative, and that count is reported rather than hidden: it measures how far the zone-by-zone markup
moves zones relative to a coupling the LP had ordered the other way.

## The flexibility bid sets the price level (`DISPATCH_FLEX_VOM`, 2026-08)

`cap_flex_gw` adds one dispatchable block per zone — battery + demand response + H2 peaker — bid at
`VOM["flex"]`. Once adequacy is closed, that bid stops being a backstop and becomes **the price**.

Measured on the 2027-46 projection, the French SMC in 2046 takes essentially four values:

| SMC | hours | share | marginal unit |
|---|---|---|---|
| **300.0** | 3129 | 36 % | the flex block, at exactly its VOM |
| −0.0 | 1867 | 22 % | RES surplus |
| 7.0 | 1413 | 16 % | nuclear |
| 201.0 | 500 | 6 % | gas |

The 300 band carries **71 % of the annual mean**. So a single default — one the `stacks/costs.py` comment
already flags as "a DEFAULT, not a revealed-behaviour measurement" — sets more than a third of French hours
and most of the price level.

Those hours are **nightly, not seasonal**: the hour-of-day histogram peaks at 23:00 (257 h) and is
essentially empty between 10:00 and 14:00 (1-3 h), with RES averaging 18.2 GW against 48.0 GW in the
cheapest half of the year. This is why RES capacity is the wrong lever on the price LEVEL: scaling French
RES to the PPE3 high end (+27 %) removes 121 of those 3129 hours, and doubling it still leaves 1476. Solar
cannot clear a 22:00 price. What the RES trajectory does move is the cheap tail — negative hours, capture
rates, and therefore RES revenue.

`costs.flex_vom()` reads `DISPATCH_FLEX_VOM` at every call so the level can be swept. The effect is not a
simple re-pricing of the affected hours: at 180 the block sits BELOW gas (~201 €/MWh in 2046) rather than
above it, so it displaces gas instead of being displaced by it and the marginal unit changes identity. That
has to be measured, not extrapolated from the base run.

## Remaining work

Projection-mode neighbours (weather-regression demand, RES CF transfers, TYNDP capacities) for 2027–2046;
real time-varying NTCs + ENTSO-E installed capacity; PSP storage; full projection engine (50 draws,
partitioned Parquet) + the §8 structural/physics metrics (per-tech generation ±10 %, net-export ±15 TWh,
per-border flow duration). See `DECISIONS.md`.
