# dispatch_model (step vi) — methodology note

Multi-zone economic dispatch producing **hourly zonal system marginal prices** for a 7-zone European
footprint (FR unit-resolved; DE-LU/BE/CH/IT-North/ES aggregated; GB as a border curve). Consumes steps
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
Scarcity is priced inside the LP — DSR tranches (300/1000/4000 €/MWh as high-SRMC "units"), unserved
energy at VoLL, RES curtailment at `res_bid<0`, over-generation `dump` at the price floor — so negative
and scarcity prices, and cross-border spreads, are **duals, never post-processed**.

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
- **Neighbour capacity** = p99.9 of observed generation (availability proxy); ENTSO-E installed capacity
  is the exact input (TODO). Undersizing *inflates* scarcity; DSR tranches cap it.
- **Backtest availability**: FR nuclear from rolling-max output; REMIT (task #41) is the ground-truth
  upgrade. GB via a fixed border curve (no ENTSO-E data post-Brexit).
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

## Remaining work

Projection-mode neighbours (weather-regression demand, RES CF transfers, TYNDP capacities) for 2027–2046;
real time-varying NTCs + ENTSO-E installed capacity; PSP storage; full projection engine (50 draws,
partitioned Parquet) + the §8 structural/physics metrics (per-tech generation ±10 %, net-export ±15 TWh,
per-border flow duration). See `DECISIONS.md`.
