# RES negative-price bidding — design (step vi LP + step vii prerequisite)

**Status:** design agreed, not yet implemented. Scope: all 7 zones (FR, DE_LU, BE, GB, CH, IT_NORTH, ES).
**Why this exists:** the LP bids all must-take RES at a single flat `res_bid = -10 €/MWh`. That is wrong in
both directions and it is *not* a markup problem — it is a missing mechanism, so it must be fixed in step
(vi) before step (vii) fits anything, or the markup will absorb it and carry it into every future scenario.

## 1. Evidence (2019 backtest, post gas-basis + must-run fixes)

| | model | observed |
|---|---|---|
| DE-LU negative hours | 665 | 210 |
| **mean price when DE < 0** | **exactly −10.0** | **−17.2** |
| FR / BE / CH negative hours | 0 / 0 / 0 | 27 / 71 / 17 |
| share of FR/BE/CH negatives that are DE-coincident | — | **85 % / 79 % / 82 %** |

Three separate readings:
1. The model's negatives are **degenerate** — pinned exactly at the single `res_bid`. Reality has a
   *distribution* (mean −17.2) because RES has a real supply curve at negative prices.
2. Neighbour negatives are overwhelmingly **imported contagion**, which the model cannot transmit (DE sits
   at −10 while FR and BE both sit at +13.1 — *identical*, i.e. FR↔BE free, DE↔FR and DE↔BE saturated).
3. IT/ES print 0 negatives **by regulation**, not by economics (see §5).

## 2. The mechanism (this is the crux)

Under Germany's **sliding market premium** the plant is paid `AW − Monatsmarktwert`, where `AW`
(*anzulegender Wert*) is its statutory tariff or auction strike. Hourly revenue is `spot_h + premium`, so it
rationally generates while

    spot_h > −premium ,   premium = AW − MW_month

Two distinct consequences — I originally conflated them:

- **A supply curve.** `AW` varies by vintage and auction round ⇒ **every vintage curtails at a different
  negative price**. 2019 solar `AW` ≈ €50–70, `MW_month` ≈ €35–40 ⇒ floors ≈ **−€10 … −€30**. That spread is
  exactly why the observed mean negative price is −17.2 and a flat −10 cannot reproduce it.
- **A stateful trigger (§51 EEG).** After **N consecutive** negative hours the premium is cancelled, the
  floor jumps from `−premium` to ≈ 0, and that capacity curtails. N tightens over time:
  **6 h (2017) → 4 h (2021) → 3 h (2025) → 2 h (2026) → 1 h (2027)**; the *Solarspitzengesetz* (2025)
  removes payment for new solar in **any** negative hour.

**The trigger is path-dependent**, so it cannot be a static supply curve inside a window LP. Model it as a
**fixed point**: solve → find negative-price runs → for tranches whose run-length exceeds their N, set the
floor to ≈0 → re-solve. 2–3 iterations converge. (A MILP with run-length logic is the exact formulation and
is far too heavy for 52 windows × N years.) Cost accepted by the user.

## 3. Data model: plant → tranche

Target artifact: `res_bid_stack(zone, tech, year) -> [(volume_mw_share, bid_floor, trigger_hours), …]`,
consumed by the LP in place of the scalar `res_bid`.

| step | how |
|---|---|
| **plant registry** | per-country source (§4) → `plant_registry` lake dataset: `plant_id, zone, tech, capacity_mw, commissioning_date, scheme, aw_eur_mwh, support_end` |
| **scheme** | **derived by statutory rule**, not a registry column (registries do not label it) |
| **bid floor** | FiT → price-**insensitive** (paid regardless ⇒ produces always, deep floor) · market premium → `−(AW − MW_month)` · post-support / merchant → ≈0 · CfD → ≈0 (no payment in negative hours) · certificate schemes → `−(certificate value)` |
| **trigger N** | by scheme **and year** (see §2 timeline) |
| **roll-off** | `support_end = commissioning + 20 y` (DE; first expiries hit 1 Jan 2021) ⇒ plant drops to the merchant tranche automatically |

**The long-term trend is therefore *derived, not assumed*.** For year Y, any plant with
`commissioning + term ≤ Y` is merchant; new build from the RES capacity trajectory enters under the
*prevailing* scheme for its vintage (EEG 2027: CfD + mandatory direct marketing, 1-hour trigger). No fitted
constant, and 2040 differs from 2019 for a legible legal reason.

### DE scheme-assignment rule (from commissioning date + capacity + auction flag)
- `< 2012` → FiT (Einspeisevergütung)
- `2012–2015` and `> 500 kW` → market premium (direct marketing)
- `2016+` and `> 100 kW` → market premium (mandatory direct marketing)
- `< 100 kW` → FiT (until EEG 2027, which makes direct marketing universal)
- auction award present (2017+) → market premium at the **auction strike** (`Zuschlagswert`)
- `commissioning + 20 y` reached → **merchant**

## 4. Registries per zone

| zone | registry | plant-level? | notes |
|---|---|---|---|
| **DE_LU** | **Marktstammdatenregister (MaStR)** via `open-mastr` (bulk, few GB, DL-DE-BY-2.0) | yes | `solar_extended`+`solar_eeg`, `wind_extended`+`wind_eeg`: commissioning date, net capacity, EEG ID, auction award. **The anchor** — carries ~all the negative-hour signal. `AW` strikes join from BNetzA auction results. |
| **FR** | **ODRÉ `registre national`** (RTE; data.gouv.fr) | ≥36 kW; **<36 kW aggregated only** (2016 decree, separate IRIS dataset) | has `filiere`, `technologie`, `regime`, commissioning date; **yearly historical snapshots** — useful for backtest vintages. *Complément de rémunération* stops paying during negative hours. |
| **BE** | VREG (Flanders) / CWaPE (Wallonia) certificate registries | partial | **Highest-value after DE**: green certificates pay per MWh *regardless of price* ⇒ floors ≈ **−€65–90**. Very likely why a small system shows 71 negative hours. |
| **GB** | Ofgem **Renewables & CHP Register** (RO/REGO) + **REPD** | yes | ROCs paid per MWh regardless ⇒ deep floor; CfD has a **6 h** negative-price rule. |
| **CH** | **Pronovo** (KEV); OPSD carries a CH **tariff (CHF)** field | partial | small RES; CH negatives are ~all imported. |
| **IT_NORTH** | GSE (Atlaimpianti); Conto Energia / FER1 | partial | CfD support cut after **6 consecutive** negative hours. |
| **ES** | RAIPRE / MITECO | partial | RECORE + large merchant/PPA share. |

**Cross-check source:** OPSD `renewable_power_plants` — harmonised schema, but **only CZ/DK/FR/DE/PL/SE/CH/UK
(no BE/ES/IT)** and **frozen at 2020-08-25**, so it misses the entire post-2020 solar boom. Use it as a
validation cross-check and for CH tariffs; not as the backbone.

## 5. Market rules that are *not* subsidy (fix regardless)

- **IT_NORTH: negative prices prohibited until the TIDE reform (Jan 2025).**
- **ES: negative prices only permitted from Dec 2023.**
⇒ Their 0 observed negative hours in 2019 are a **hard regulatory floor at 0**, not economics. The model
currently prints **6 spurious negative ES hours in 2019**. Needs a per-zone, time-varying price floor
(`res_bid` and dump cost = 0 for those zone-years). Critically, **the floor is gone in both zones for the
2027–46 projection**, so history and future are governed by different rules — a markup fitted on floored
history would be badly wrong for 2040.

## 6. Build phases

1. `pricemodeling/registries/` ETL (same pattern as the RTE extract) → `plant_registry` lake dataset.
   Order: **MaStR** (anchor, proves the chain) → ODRÉ → BE → GB → CH → IT → ES.
2. Scheme-assignment rules + `AW` join (auction results / statutory tariff by vintage) → per-plant floor.
3. `res_bid_stack(zone, tech, year)` builder + workbook overrides (`dispatch_res_schemes` tab).
4. LP: tranche bids replacing scalar `res_bid`; **trigger fixed-point loop**; per-zone/per-year price floor (§5).
5. Re-backtest 2019 + multi-year; acceptance on **negative-hour count *and* the negative-price distribution**
   (mean/quantiles), not just baseload.
6. Only then fit the step-(vii) markup on what genuinely remains.

## 6b. MaStR findings (2026-07, DE thermal fleet — 93,763 units in the `reference` layer)

Two things only visible unit-level, both corrections to earlier guesses:

- **The literature must-run fractions were wrong in both directions.** Measured CHP-electrical share of
  installed capacity: **lignite 0.09** (guess was 0.45 — 5× too high; German lignite is condensing, not
  CHP), **coal 0.30** (guess 0.35 — right), **gas 0.60** (guess 0.15 — 4× too *low*; German gas is heavily
  municipal/industrial CHP). Total measured DE CHP-electrical ≈ 31 GW.
  - **Two data traps en route, both fixed:** (1) the binary "has a `KwkMastrNummer`" flag tags 14.2 of
    14.8 GW of lignite as CHP, but their actual CHP-*electrical* capacity is only 2.3 GW — a 1060 MW block
    carries 20 MW of district-heating extraction. Must-run is driven by `chp_el`, not the flag. (2) A
    `KwkMastrNummer` is a CHP-*plant* registration linking several units; a naive per-unit join gave 58 GW
    of gas CHP against 36 GW installed. Fixed by allocating each registration's `chp_el` across its linked
    units proportional to size, capped at unit capacity (`_allocate_chp`).
  - **`chp_el` is a capacity, not an hourly must-run.** Heat obligation is seasonal (≈full DJF, ≈0 summer),
    so the dispatch floor is `chp_el(tech) × heat_shape(month)`. Applying it flat year-round is what makes
    the annual level sag — arguably the real mechanism behind the −12 pp, more than the absolute level.
- **The Sicherheitsbereitschaft / grid-reserve exclusion hypothesis is DEAD from MaStR flags.**
  `NetzreserveAbDatum` 0 non-null, `SicherheitsbereitschaftAbDatum` only 7. The 2016-20 standby-lignite
  units are permanently closed by the 2026 snapshot, so they surface via `retirement_date` + the
  `active(year)` vintage filter, not a reserve flag. Honest negative result.

## 6c. Implementation status (step vii, 2026-07)

**Built + tested (6 unit tests + 13 LP tests green):**
- LP (`lp/multi_zone.py`) takes an optional `res_tranches` — RES becomes a per-scheme **supply curve**
  (each tranche `0 ≤ res_k ≤ share_k · res_pot`, bid at its floor) instead of one block at −10. Back-compat:
  no `res_tranches` ⇒ the old flat behaviour. Also hardened for the single-zone / no-border case.
- `res_schemes.py`: workbook loader (`dispatch_res_schemes` tab, shares normalised) + the **§51 fixed
  point** (`solve_with_triggers`). The trigger is **sticky** — once a tranche-hour loses its premium in a
  negative episode it stays lost; a non-sticky rule oscillates (zeroing a premium lifts the price to 0,
  which then looks non-negative and would restore it). Monotone accumulation converges in 2–3 solves.
- Tranche shares seeded from the registry for DE (wind ~72 % market-premium / 26 % FiT; solar ~64 % FiT),
  sourced estimates elsewhere; per-country floors + triggers in the editable tab (BE green certificates
  −80/no-trigger, FR CR −5/1h, GB ROC −45 + CfD 6h, …).

**Verified behaviour:** the supply curve produces negative prices at the *marginal tranche floor* (min −20,
not a flat −10) and the §51 trigger lifts a tranche to 0 after its N-hour run — both unit-tested.

**Open calibration issue (honest):** on the 2019 backtest the mechanism gives the right *depth/shape* but
**too few negative hours** (DE ≈ 26 vs 210 observed — it *reduced* them vs the crude must-run proxy). A
subsidised negative RES bid makes the LP prefer to keep RES on and **export to DE_REST / dump** rather than
curtail, so DE only prices negative when the *whole* region is in surplus. Suspects, in order: (1) the
DE↔DE_REST flow-NTC (p99.5) is too generous, letting DE always export its surplus; (2) DE_REST wind is not
correlated tightly enough with DE hour-by-hour (it should be — same weather), so it absorbs when it
shouldn't; (3) trigger/threshold tuning. This is the next diagnosis, not a mechanism bug.

## 6d. Diagnosis — why too few negative hours (2026-07)

**Root cause: the flow-NTC overstates *coincident* export in region-wide-surplus hours.** Observed 2019:
in DE's 211 negative hours DE was already exporting **~12 GW (p95 14.7 GW)** across all borders and *still*
cleared negative — its surplus exceeded what it could push out. The model gives DE **~18–20 GW** of export
headroom (the sum of each border's *all-hours* p99.5 from `flow_derived_ntc`). But those per-border peaks
occur at **different** times; in a windy hour every border exports the same way and congests **together**,
so the achievable simultaneous export is far lower. That ~5 GW of phantom capacity lets the model clear DE's
surplus instead of pricing it negative. Neighbour net-loads are in fact well-correlated with DE (PL +0.66,
NL +0.70), but PL/CZ are coal with ~no wind, so their "surplus" still clears at positive SRMC and they keep
importing DE's excess up to the (overstated) NTC. The RES bid stack compounds it: deeper subsidy floors make
RES export harder before curtailing (negatives 85 flat-bid → 21 tranched).
- **Not a mechanism bug**; the level (DE −7.1 %) and correlation (0.75) don't depend on the tail. Fix is a
  **coincidence factor** on NTC in surplus hours (or, more correctly, splitting DE_REST into wind-correlated
  DK/AT vs coal PL/CZ). A negatives-tail calibration — deferred, quantified here.

## 6e. Projection-time scheme evolution — **NOT yet modelled (required before the 20-yr run)**

Verified: `load_res_schemes` returns **static** shares (no year dimension) and there is **no dispatch
projection engine** — only the backtest. So a 20-year simulation would freeze the 2019 subsidy structure
into 2046, which is badly wrong and first-order for future negative prices:
- EEG plants hit **commissioning + 20 y → merchant** (bid ≈0). 2019-vintage FiT solar is merchant by 2039.
- New build (from the RES capacity trajectory) enters under the **prevailing** scheme for its vintage —
  EEG 2027 ⇒ **CfD + mandatory direct marketing, 1-hour trigger** (bid ≈0, tight self-limiting).
- The §51 trigger tightens **6 h (2019) → 3 h (2025) → 2 h (2026) → 1 h (2027)**.

Net: the deep-subsidy tranches (FiT −60, market-premium −20) **shrink** and the merchant/CfD tranches
(≈0, 1-h trigger) **grow**, so future negatives get **shallower and shorter**. The static tab would instead
carry 2019's deep floors + 6-h trigger to 2046 → systematically over-deep, over-frequent future negatives.

**The registry was built for exactly this** — `support_end` (commissioning+20), `scheme` by vintage,
`active(year)` — so the year-varying mix is *derivable*, not fittable.

**Built (2026-07):** `dispatch_model/scheme_evolution.py` — `scheme_shares(zone, year, floors, new_build)`
derives the tranches for any year from `registry.active(year)`, applies the roll-off
(`support_end ≤ year ⇒ merchant`), takes new build under the prevailing scheme, and attaches the §51
trigger from `trigger_hours(year)` (6 h ≤2020 → 1 h 2027+). Floors stay the workbook economic constants.
The registry now stores the **statutory** scheme + `support_end` (the roll-off is applied per projection
year, not baked against today — a bug fixed here). Demonstrated on the real DE fleet (registry-derived):

    year  §51   FiT(−60)  mkt-prem(−20)  merchant(0)
    2019  6h      21%        53%           27%
    2030  1h       6%        67%           27%
    2039  1h       0%        29%           70%
    2046  1h       0%         0%          100%

10 unit tests green (roll-off, trigger schedule, new build, merchant-never-triggers).

**Still to build to actually *run* the 2027-46 simulation:** (1) the **dispatch projection engine** (step-vi
projection mode was never built — the LP/trigger machinery is ready, it just needs a driver that calls
`scheme_shares(zone, year)` per year instead of the static tab); (2) **TYNDP** future RES capacity to feed
`new_build_mw` (today's demo is existing-fleet roll-off only); (3) registries for the non-DE zones (they
fall back to the workbook mix); (4) solar completion to sharpen DE's base-year split.

## 6f. Registry coverage by zone (task #70) — tiered by data quality

The scheme-evolution machinery is zone-agnostic; only the **data** differs. There is no single open feed
with long vintage history for all zones, so coverage is tiered (ADR-7's honest-asymmetry principle):

| zone | source | tier | status |
|---|---|---|---|
| DE_LU | **MaStR** (open, plant-level, EEG vintages) | plant-level | **done** — 170k units; FiT/mkt-prem |
| FR | **ODRÉ** (RTE, plant-level ≥36 kW, <36 kW aggregated) | plant-level | **done** — 137k rows; nuclear 61.4 / hydro 26 / wind 22.8 / solar 22.4 GW match known totals; OA/CR |
| GB | **REPD** (UK gov; `Operational` rows, excl. NI; `CfD Capacity` column) | plant-level | **done** — 2.75k units; offshore 13.6 / onshore 13.3 GW match; ROC/CfD |
| CH | **OPSD** `renewable_power_plants` (CH commissioning + `contract_period_end`) | plant-level (2020) | **done** — small fleet; KEV. Stale (misses 2020+) + KEV-only coverage — documented |
| BE | VREG/CWaPE — no open per-plant feed | cohort (workbook, sourced) | **done** — green certificates; 4 cohorts |
| IT | GSE Atlaimpianti — patchy | cohort (workbook, sourced) | **done** — Conto Energia / merchant; 4 cohorts |
| ES | RAIPRE — patchy | cohort (workbook, sourced) | **done** — RECORE / merchant; 4 cohorts |

**All 7 zones evolve.** 4 plant-level ETLs (`registries/mastr|odre|repd|opsd.py`) + 1 cohort loader
(`registries/cohort.py`) reading the sourced `dispatch_res_vintages` tab. Plant-level totals validate
against known national fleets; the cohort tier is explicitly sourced estimates to refine, not measurements.

Scheme is **derived statutorily** per country (registries label status, not scheme): DE EEG FiT/market-
premium; FR obligation d'achat (<2016 or ≤100 kW) / complément de rémunération (its clause suspends
payment at negative prices ⇒ ≈0 floor); GB ROC (paid regardless, deep) / CfD (6 h rule); etc. The cohort
tier feeds the *same* `scheme_shares` via capacity-by-(vintage, scheme) rows in the registry rather than
per-plant rows, so the downstream code is identical.

## 7. Known risks / open questions

- **MaStR does not state the scheme** → everything rests on the statutory rule being right; needs a sanity
  check against the known ~15 % FiT / 75 % premium payment split (2023).
- `MW_month` (monthly market value) is **endogenous** to the model's own prices — decide whether to use the
  historical published Monatsmarktwert (backtest) or the model's own monthly mean (projection).
- BE/ES/IT registries are patchier than DE/FR; may need national-aggregate fallbacks with explicit sourcing.
- FR <36 kW is aggregate-only → small rooftop solar enters as a single tranche, not per-vintage.
- Certificate values (BE) and ROC values (GB) are themselves time-varying and need their own small series.
