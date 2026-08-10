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
- **Neighbour capacity** = p99.9 of observed generation (availability proxy); ENTSO-E installed capacity
  is the exact input (TODO). Undersizing *inflates* scarcity; DSR tranches cap it.
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
