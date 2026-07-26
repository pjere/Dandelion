# Plant operating rigidities & downward bid ladder (FLEX module)

> Endogenous negative-price formation for the dispatch LP. Extends the model with (A) the operating
> rigidities of nuclear and fossil plants and (B) a correct downward bid ladder, so negative prices emerge
> as balance duals with the right count, depth, timing and fleet behaviour. **Pure LP, no binaries** — every
> rigidity is a continuous variable + linear coupling constraint, so the hourly balance duals stay valid
> prices. Opt-in behind `flexibility.enabled` (config.yaml); **byte-identical when off** (golden preserved).
> Built in phases FLEX-F0…F8 (see the plan). This doc is the living spec; the full algebraic LP statement
> and the calibration report land in F8.

## LP statement (new variable & constraint families)

Per FR dispatchable unit *i*, hour *t* (all intertemporal constraints inside the rolling window; only state
crosses seams as fixed parameters — F5):

- `u[i,t] ∈ [0, avail[i,t]]` — committed capacity (continuous LP relaxation of commitment).
- `p[i,t]` — output; `d[i,t] ≥ 0` — nuclear deep-modulation depth; `su[i,t] ≥ max(0, u[t]−u[t−1])` — start.

| id | constraint (linear) | phase |
|----|--------------------|-------|
| C1 | `p ≥ α_band·u − d`, `d ≤ (α_band − α_tech)·u` — two-tier minimum (band floor + deep band) | F2a |
| C2 | `Σ_{k=0..7} d_{t−k} ≤ D_max8h·cap`, `Σ_{t∈day} d ≤ D_max_day·cap`, cost `c_mod·d` in objective | F2b |
| C3 | `p_t − p_{t−1} ≤ R_up·u_t − β·Σ_{k=1..8} d_{t−k}` — xénon up-ramp asymmetry | F2b |
| C4 | maneuverability {full / reduced (½ caps) / none (`p = avail·u_frozen`, must-run stretch-out)} | F3 |
| C5 | start cost `c_start·su` (F2a); min-down proxy `u_t ≤ u_{t−1} + avail·ρ_recommit` (F2b) | F2a/b |
| C6 | reserve `Σ(avail·u − p) ≥ R_up_req`, footroom `Σ(p − (α_band·u − d)) ≥ R_down_req` (nuclear + hydro) | F3 |
| C7 | grid-stability must-run `Σ_{i∈nuc} p ≥ P_minstab[zone, year]` | F3 |
| §4 | fossil `p ≥ α_min·u` + blended start cost + persistence | F3 |
| §6 | downward bid ladder: CR/merchant ≈0…−5, legacy OA at floor decaying by vintage expiry | F4 |

Deep-mod *budget* is an explicit `c_mod` **cost** (not an annual hard budget) so windows stay independent.
Duration is the **energy proxy** `Σ d ≤ D_max8h` (no indicator variable → stays LP). Nuclear's implicit
negative bid emerges from C2/C5 — never added explicitly.

## Physics vs regime classification (spec §1.3)

Time-invariant **physics** are hard-coded per reactor class (`flexibility/reactor_class.py`); economically/
regulatorily contingent **regime** parameters are year-indexed workbook trajectories
(`flexibility/trajectories.py`, `dispatch_*` tabs) — never constants frozen over the 2026–46 horizon.

| parameter | kind | where | source |
|-----------|------|-------|--------|
| α_band, α_tech (band & technical minima) | physics | `reactor_class` | IAEA/NEA load-following lit.; EDF mode-G RGE |
| R_up, R_down, xénon β | physics | `reactor_class` | NEA 2011; xénon transient dynamics |
| ρ_recommit (min-down recommit ramp) | physics | `reactor_class` | hot-standby → full-load restart time |
| D_max8h, D_max_day (deep-mod caps) | physics (level) / calibrated (scale) | `reactor_class` + F7 | design envelope; fit to ~30–33 TWh/yr |
| maneuverability, stretch-out profile | physics (derived) | availability_model (F1) | fuel-cycle position (scheduler / REMIT gaps) |
| fossil α_min (min stable load, §4) | physics | `fr_nuclear._FOSSIL_MIN_LOAD` | CCGT/coal/OCGT stable-generation floors |
| c_mod (€/MWh on deep-mod) | regime/calibrated | `dispatch_flex_costs` | EDF 2026 modulation study; F7 |
| c_start (€/MW on recommit) | regime/calibrated | `dispatch_flex_costs` | NEA cycling cost; F7 |
| P_minstab (grid-stability floor) | regime | `dispatch_minstab` | RTE–EDF / CRE 2026 minimum-injection |
| R_up_req, R_down_req (reserves) | regime | `dispatch_reserves` | RTE reserve dimensioning |
| OA/CR capacity by vintage, expiry | regime | `dispatch_res_vintages` (existing) | CRE/EDF OA statistics |
| ladder bid levels, market floor/cap | regime | `dispatch_oa_ladder` | EU day-ahead floor/cap rules |

## Workbook tabs (long schema `[…, year, value]`; absent tab ⇒ documented default)

| tab | columns | meaning |
|-----|---------|---------|
| `dispatch_flex_costs` | variable, year, value | `c_mod`, `c_start_<class/tech>` (€/MWh, €/MW) |
| `dispatch_minstab` | zone, year, value | grid-stability nuclear must-run `P_minstab` (MW) |
| `dispatch_reserves` | variable, year, value | `r_up_req`, `r_down_req` (MW) |
| `dispatch_oa_ladder` | variable, year, value | `cr_bid`, `oa_bid`, `market_floor`, `market_cap` (€/MWh) |
| `dispatch_res_vintages` | (existing) | RES capacity by (zone, tech, vintage, scheme) → OA/CR expiry |

Reactor-class physics seeds and defaults are in the two `flexibility/` modules; the free calibration
parameters (`c_mod`, `c_start`, `β`, D-caps, ε) are fitted in F7 to the §9 acceptance targets.

## Status

- **F0 (done):** package skeleton, `flexibility.enabled` flag (default off), `reactor_class` physics
  registry, `trajectories` regime loaders with defaults, this classification table. No LP change → golden
  untouched. Tests: `tests/test_flexibility_scaffold.py`.
- **F1 (done):** `maneuverability.py` derives per-unit-week `{full, reduced, none}` + stretch-out power from
  fuel-cycle position — a pure function of an outage calendar, fed by `planned_scheduler` (projection) or
  `backtest_calendar` reading `entsoe_unavailability` (backtest). Validated on real 2024 REMIT data: fleet
  distribution 93.9 % full / 4.4 % reduced / 1.7 % none, stretch-out peaking Apr & Aug (before the refuel
  season) — the expected shape. Tests: `tests/test_flexibility_maneuverability.py`. No LP change → golden
  untouched. (eic→dispatch-stack unit matching is done at LP build time, F2/F3.)
- **F2a (done):** the LP rigidity **core** in `lp/highs_solver._build` behind the opt-in `flex` spec —
  columns `u` (commit ∈[0,avail·cap]), `d` (deep-mod, cost `c_mod`), `su` (start, cost `c_start`); rows
  `p≤u`, C1a `α_band·u−d−p≤0`, C1b `d−(α_band−α_tech)·u≤0`, C5 `su≥u_t−u_{t-1}`. Pure LP → duals stay
  prices. Proven **byte-identical when off**. Toy tests (`tests/test_flexibility_lp.py`): a committed reactor
  forced into a trough deep-modulates to a **negative dual = srmc−c_mod**, depth scales with `c_mod`, and no
  recovery ⇒ no negative (C5 coupling). Key finding: C1–C3 are inert without C5's stickiness.
- **F2b (done):** the intertemporal rationing that gives the negatives the right depth/count/timing, plus
  the end-to-end wiring. In `lp/highs_solver._build`, four more rigidity families, each opt-in on its spec
  key so an F2a spec stays byte-identical: **C2a** `Σ_{k=0..7} d_{t−k} ≤ d_max_8h·cap` (rolling 8 h) and
  **C2b** `Σ_{t∈day} d ≤ d_max_day·cap` (calendar-day) deep-mod energy budgets on nominal cap; **C3**
  `p_t−p_{t−1} ≤ r_up·u_t − β·Σ_{k=1..8} d_{t−k}` xénon up-ramp asymmetry; **C5 min-down**
  `u_t−u_{t−1} ≤ avail·ρ_recommit`. `rho_recommit` added to `reactor_class` physics. `flex` now threads
  `solve_multizone`→`solve_with_triggers`→`run_backtest`/`project_year` (highs-only; linopy path raises).
  New `flexibility/fr_nuclear.py` builds the **real per-reactor FR nuclear stack**: keeps the ~56 unit rows
  (skips the `nuclear_curve` tranche surrogate), prices each reactor along the revealed curve **floored at
  the fuel cost** (the sub-zero socle bid is *not* written in — negatives stay endogenous, per the §LP note),
  attaches per-class physics + year regime costs, and accepts F1 maneuverability (deep-mod cap derate;
  full C4 is F3). `run_backtest(flexibility=…)`/config `flexibility.enabled` gate it; the solver also exposes
  the `u`/`d`/`su`/`p` primal (read-only) for validation. Tests: `tests/test_flexibility_lp.py` (9, one per
  family + budget/ramp/min-down bite) and `tests/test_flexibility_fr_nuclear.py` (5). Proven still
  byte-identical when off.

  *Backtest validation (2024, weeks 0–14, `scratchpad/f2b_negweek_validate.py`).* The toy LP tests prove the
  supply side produces **clean endogenous negatives** (`srmc − c_mod`). In the full 7-zone 2024 backtest the
  effect is the **degeneracy break**: FR's min price falls from a flat **7.0** (the nuclear-bloc pin) to
  **0.0** across the spring surplus weeks (11–14) as the fleet follows load down to its C1 band floor. It
  does *not* yet cross sub-zero there — with `c_mod`=8 the deep-mod reservation is only −1, and the residual
  surplus is absorbed by 0-floored RES + the export limit **before** deep-mod becomes marginal. This is
  exactly the S1c export lock the committed `stacks.nuclear_curve` analysis flagged ("le verrou n'est pas le
  prix d'offre nucléaire mais le mécanisme d'export… à ne pas ré-attribuer au nucléaire"): F2b removes the
  nuclear-supply degeneracy but the FR negative *tail* stays gated downstream (the F4 downward ladder + the
  S1c export mechanism), not by nuclear supply. So C1 binds in 2024; the C2/C3 deep-mod machinery is proven
  on the toy LP and waits on a deeper-surplus regime (or the F4/F7 depth calibration) to bind in the backtest.

  **NB — REMIT→stack unit matching is not yet wired**, so the backtest passes maneuverability `None` (every
  reactor `full`); real per-week maneuverability activation lands with that matching layer in F3.
- **F3 (done):** the rest of the rigidity set + fleet-level constraints, all in the same pure-LP,
  opt-in-per-key pattern. In `lp/highs_solver._build`: **C4 maneuverability** — `deepband_scale` (reduced→½,
  none→0) scales the C1b deep band, and `must_run_frac` pins a `none` reactor's output at its stretch-out
  power (`p = stretch·avail·cap`, a must-run coast-down with no modulation); **C6 reserves** —
  `Σ_nuc(u−p)+Σ_res(cap−p) ≥ r_up_req` (upward headroom) and `Σ_nuc(p−α_tech·u)+Σ_res p ≥ r_down_req`
  (downward footroom, measured above the *technical minimum* — measuring above the deep-mod floor `α_band·u−d`
  would let the LP raise `d` to fabricate footroom, incentivising deeper modulation, so that literal reading
  is deliberately not used); **C7 grid-stability** — `Σ_{nuc} p ≥ p_minstab[zone]`. An `is_nuclear` mask +
  `reserve_idx` (hydro) let C6/C7 span the right sub-fleets. **§4 fossil** — `fr_nuclear._append_fossil`
  attaches commitment rigidity (min stable load `α_min`, tech start cost, fast recommit ramp, real per-tech
  up-ramp) to the FR thermal rows as a combined spec with `is_nuclear=False`, so the nuclear-only families
  skip them; only C1 (min load) + C5 (start) + min-down + C3 (real ramp) bite. The REMIT→stack matching that
  F2b flagged turned out to be a **plain plant-name join** (REMIT `unit_name` = fleet `name`), so C4 is now
  live in the backtest: `rolling.backtest._fr_maneuverability` builds per-week states (validated 3393 full /
  215 reduced / 84 none over 2024, matching F1) and `fr_nuclear.window_spec` re-derates the fleet window by
  window. `load_reserves`/`minstab_mw`/fossil costs all thread from `trajectories`. Tests:
  `test_flexibility_lp.py` (+5 F3 LP families), `test_flexibility_fr_nuclear.py` (+3 maneuver/fossil/reserve).
  Still byte-identical when off. Reserve seeds (1500/1000 MW) are wired **live** (they bind in tight hours);
  F7 recalibrates the levels. Projection wires C6/C7/§4 too; its C4 waits on the planned-outage scheduler.

  *Backtest validation (2024, weeks 0–14).* End-to-end clean with every F3 family active. Effects are the
  expected modest upward pressures — the week-0 min moves 7.0→4.0 and shoulder-week means nudge up a few
  tenths as reserves hold head/footroom and fossil commitment keeps min-load on. The spring surplus weeks
  still floor at 0 with no sub-zero: the FR negative tail stays export-gated (S1c), unchanged by any
  supply-side rigidity, exactly as F2b found — its unlock is F4 (downward ladder) + the export mechanism.
- **F4 (done):** the §6 **downward bid ladder** for FR. Most of the ladder already existed — the FR
  `dispatch_res_schemes` tab carries CR/OA/merchant tranches, the §51 trigger tightens 6h→1h, and
  `scheme_evolution.scheme_shares` decays the OA *volume* by 20-year vintage expiry (FR RES is in the
  registry with `support_end`). F4 makes the FLEX module **own the bid *levels*** via
  `trajectories.apply_oa_ladder` + `load_oa_ladder` (`dispatch_oa_ladder` tab; sourced defaults when absent),
  reconciled against RES_BIDDING_DESIGN.md: `obligation_achat` is a legacy feed-in tariff **paid regardless
  of price** ⇒ it bids at the **market floor** (−500, "legacy OA at floor" — the old static −40 was a bounded
  proxy); `complement_remuneration`'s premium is **suspended at negative prices** ⇒ it bids **≈0** (−1, within
  the §6 "≈0…−5"); `merchant`→0. Every bid is truncated to `[market_floor, market_cap]` (EU day-ahead bounds).
  Shares/triggers are untouched, so the vintage decay still governs the OA volume — the ladder only sets the
  price each surviving tranche bids at. Wired into `run_backtest`/`project_year` when FLEX is on (flex-off is
  byte-identical: the static tab is untouched). Tests: `tests/test_flexibility_oa_ladder.py`.

  *Backtest validation (2024, weeks 0–14).* Byte-for-byte identical to F3 — no regression, and crucially **no
  spurious −500 prints** despite OA now bidding at the market floor. FR still floors at 0 in the surplus
  weeks: because the tail is export-locked (S1c) the price never descends past 0 to where OA/CR would be
  marginal, so their (now correct) bid levels are inconsequential to the 2024 backtest. The corrected ladder
  bites only once the export lock is lifted (S1c) and in the projection (deeper future surplus).
- F5…F8: see the phase tasks and the work plan.
