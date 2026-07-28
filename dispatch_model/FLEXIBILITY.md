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
| `dispatch_flex_costs` | variable, year, value | `c_mod`, `c_start_<class/tech>`, `u_commit_frac` (κ), `alpha_band_op` (€/MWh, €/MW, fractions) |
| `dispatch_minstab` | zone, year, value | grid-stability nuclear must-run `P_minstab` (MW) |
| `dispatch_reserves` | variable, year, value | `r_up_req`, `r_down_req` (MW) |
| `dispatch_oa_ladder` | variable, year, value | `cr_bid`, `oa_bid`, `mer_bid`, `market_floor`, `market_cap` (€/MWh) |
| `dispatch_res_vintages` | (existing) | RES capacity by (zone, tech, vintage, scheme) → OA/CR expiry |

Reactor-class physics seeds and defaults are in the two `flexibility/` modules; the calibrated values
(F7, all measured/micro-founded — see `FLEX_CALIBRATION_2024.md`) are the seed defaults, workbook-overridable.

## Complete algebraic statement (F8)

Per zone *z*, hour *t* of a weekly window (times `T`, |T| = n). Decision variables (all continuous ≥ 0):
`g[i,t]` generation per stack row, `r[k,t]` RES tranche output, `e[t]` unserved (ENS), `w[t]` dump,
`f[b,t]`/`b[b,t]` directed border flows; per FR flex unit *j*: `u[j,t]` commitment, `d[j,t]` deep-mod,
`su[j,t]` start.

    min  Σ_z Σ_t [ Σ_i (srmc_i + ε_i)·g + Σ_k floor_k·r + VoLL·e − floor_z·w ]
       + Σ_borders Σ_t ε_flow·(f + b)  +  Σ_j Σ_t [ c_mod·d + c_start_j·su ]

    s.t.  (balance, dual = price)   Σ_i g + Σ_k r + e − w + imports_z(f,b) = D_z,t          ∀ z,t
          (bounds)                  g ∈ [minf·gcap, gcap] ;  r_k ∈ [0, share_k·res_pot] ;
                                    f ∈ [0, NTC→] ; b ∈ [0, NTC←] ;  e, w ≥ 0
          (hydro budget)            Σ_t g_hydro ≤ E_week                                     [dual = water value]

    FLEX rows, FR flex units j (nuclear + §4 fossil), all gated on the spec:
          (commit)     u ∈ [κ_j·avail·cap, avail·cap]         κ_j = u_commit_frac (0 for fossil)
          (cap)        g_j ≤ u
          (C1a)        g_j ≥ α_op_j·u − d                     α_op = max(α_band_class, alpha_band_op)
          (C1b)        d ≤ (α_op_j − α_tech_j)·s_j·u          s_j = deepband_scale (C4: full 1 / reduced ½ / none 0)
          (C2a)        Σ_{k=0..7} d_{t−k} ≤ D8_j·cap          (rolling 8 h; seam RHS − Σd_hist, clamped ≥ 0)
          (C2b)        Σ_{t∈day} d ≤ Dday_j·cap
          (C3)         g_{j,t} − g_{j,t−1} ≤ r_up_j·u_t − β_j·Σ_{k=1..8} d_{t−k}
                       β_j = min(β_class, r_up_j / (8·(α_op_j − α_tech_j)))   [the xénon ceiling]
          (C5)         su_t ≥ u_t − u_{t−1}                   (seam: su_0 ≥ u_0 − u_init)
          (min-down)   u_t − u_{t−1} ≤ ρ_j·avail_t·cap + κ_j·(Δavail_t)⁺      (outage returns pass)
          (C4 none)    g_j = stretch_j·avail·cap              (must-run coast-down pin)
          (C6 up)      Σ_nuc (u − g) + Σ_res (gcap − g) ≥ R↑
          (C6 down)    Σ_nuc (g − α_tech·u) + Σ_res g ≥ R↓    (footroom above the TECHNICAL minimum)
          (C7)         Σ_nuc g ≥ min(P_minstab, 0.98·Σ avail·cap)

    §51 fixed point (outer loop): solve → find consecutive-negative runs per tranche → floors of tranches
    past their trigger drop (stickily) to `fired_floor` (flag-off 0.0; FLEX: mer_bid) → re-solve to a
    fixed pattern. FR tranches repriced by the ladder carry trigger = 0 (the French CR suspension is
    instantaneous and lives in cr_bid itself).

    F5 seam: `u_init, p_init, d_hist[8]` from the previous adjacent window enter as CONSTANTS in the
    hour-0 rows above; infeasible seam ⇒ retry cold (F4 behaviour). Pure LP throughout — every price is
    the balance dual of a vertex solution (dual simplex + the deterministic `ε_i` SRMC tie-break ≤ 0.01).

## Deliberate exclusions (backlog, each with the expected sign of its impact)

| exclusion | why | expected sign if added |
|-----------|-----|------------------------|
| **No binaries / convex-hull pricing** | pure-LP mandate: balance duals must stay prices | whole-unit commitment would *concentrate* the κ-slack in discrete weekend shutdowns (the observed "several units shut") instead of pro-rata spreading; slightly **fewer, deeper** negatives; convex-hull pricing would lift degenerate-hour prices marginally **up** |
| **Single blended start cost** (per class, no hot/warm/cold) | data + LP simplicity | differentiated starts make weekend shutdowns cheaper than the blended cost ⇒ **more** shutdowns, marginally **fewer** shallow negatives |
| **No intra-day re-optimisation** (day-ahead LP only) | scope: day-ahead price formation | intra-day recourse would relieve some forced deep-mod ⇒ **shallower** tail |
| **National, not regional, min-stab (C7)** | CRE minimum-injection is specified nationally; no grid model | regional floors bind harder locally ⇒ **more** forced surplus hours (count **up**) |
| **Neighbour-zone rigidities & ladders (BE/CH/ES/DE)** | F7 scope was the FR fleet; the LP is already zone-agnostic (`flex={zone: spec}`) — what's missing is a **spec builder per zone**: block-level pseudo-units for BE/CH/ES nuclear with a κ floor + two-tier band (reusing the `fr_nuclear` patterns); DE thermal can go unit-level via MaStR (`build_de_unit_stack`, #73) with §4 commitment; plus each zone's **measured calibration anchors** (own revealed socle share/bid via `stacks/revealed.py`, own operating regime — BE near-flat must-run, ES κ≈1 narrow band) | the depth unlock: coupled mid-band (−5…−50) appears (count ~unchanged, depth **much closer to observed**) |
| **RES potential curtailment-censoring** | observed generation understates potential exactly on curtailed hours | deeper modelled surplus ⇒ **more and deeper** negatives |
| **C4 maneuverability in projection** | needs the planned-outage scheduler hook (F1) | stretch-out units must-run in spring ⇒ slightly **more** projected negatives |

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
- **F5 (done):** window-seam state. The rolling backtest/projection solve weekly windows independently, so
  every intertemporal rigidity (C3 ramp, C5 start, min-down, C2a 8h budget, C3 xénon) silently *reset* each
  Monday — a reactor could shut Sunday night and re-commit Monday morning for free, defeating the stickiness
  at 52 seams a year. F5 carries the previous window's tail across as **fixed parameters** (still pure LP —
  the state enters only as constants in the RHS, no new coupling): `fr_nuclear.tail_state` reads `u_init`/
  `p_init` (commit/output at hour −1) and `d_hist` (the last 8 hours' deep-mod, reversed) from the solved
  window's flex primal; `_build` closes the missing hour-0 links (C5 `su_0 ≥ u_0−u_init`, min-down
  `u_0 ≤ avail_0·ρ+u_init`, C3 `p_0−r_up·u_0 ≤ p_init−β·Σd_{−k}`) and charges `d_hist` against the first
  hours' 8h budget and xénon lookback. The backtest/projection loops thread it window→window, linking **only
  across an adjacent seam** (`prev_w1 == w0`) so a skipped/short window resets to a cold start. First window
  and flex-off are unchanged (byte-identical). Tests: `tests/test_flexibility_lp.py` (+5 seam families incl.
  the clamp), `tests/test_flexibility_fr_nuclear.py` (+1 `tail_state`).

  *Feasibility.* Two seam×hard-constraint interactions can over-constrain a window, both handled so no window
  is ever dropped: (1) a maneuverability drop across a seam can leave `d_hist` above the tightened 8h budget →
  the C2a seam RHS is **clamped ≥0** (budget spent, `d_0=0`, not infeasible); (2) a finite-horizon window can
  shed commitment at its *last* hour for free, so a low `u_init` + the seam min-down cap + the hard C6 reserve
  can be jointly infeasible → the backtest/projection **retry that window cold** (F4 behaviour) instead of
  dropping it. So F5 links every feasible seam and degrades gracefully to F4 at the ~stressed ones; a fuller
  treatment (soft reserves / a terminal commitment condition) is a candidate for F7.

  *Backtest validation (2024, weeks 0–14).* All 15 windows solve (the cold fallback caught the few
  over-constrained seams). The price effect is negligible — mean 46.2→46.3, sub-0.5 €/MWh per-week shifts
  confined to the boundary hours — because seam hours are a small fraction and FR stays export-locked at 0.
  F5's value is correctness (the rigidities no longer reset every Monday), not a price move in this backtest.
- **F6 (done):** dual quality & diagnostics (spec §8). Identical-SRMC sister units make the dispatch LP
  primal-degenerate and the balance duals (= prices) noisy. **(1) `_tie_break`** adds a deterministic
  sub-cent SRMC perturbation per unit id (blake2b hash → `[0, _EPS_TIE=0.01)`), so ties break the same way
  every run/window and no price can move by more than ε; applied **only when flex is on**, so flag-off stays
  byte-identical. **(2) Dual choice** (documented in `_get_highs`): keep dual **simplex** + the ε-perturbation
  rather than interior-point-without-crossover — an interior point's duals are an analytic-centre average that
  smears the marginal price across ties instead of naming one; simplex + ε gives a unique, stable vertex dual.
  **(3) `diagnostics.dual_oscillation`** flags hour-to-hour dual jumps that no demand/availability change
  justifies (spurious degeneracy). **(4) `diagnostics.debug_hour`** decomposes any (zone, hour) price from the
  solved primal — the marginal block, saturated borders (the export lock made visible), and each reactor's
  commit/deep-mod state with its `implied_bid = srmc − c_mod`; exposed as `out["debug"](zone, hour)` under
  `diagnose=True`. This is the tool that explains an individual negative print for the F7/F8 reports. Also:
  an empty flex spec (`idx=[]`) is now a no-op (tie-break only). Tests: `tests/test_flexibility_diag.py`
  (tie-break bound + flag-off, oscillation detector, degenerate-fleet no-oscillation, negative-print
  decomposition). **Flag-off byte-identical — proven three ways:** the 2024 flag-off price fingerprint is
  bit-for-bit equal across pre-FLEX `45bfbb0`, committed F5 HEAD, and this F6 working tree (all
  `244f004…`). So neither the FLEX module nor F6 touches flag-off. `tools/golden.py check` currently flags
  only `backtest_prices/year=2024` (~5 % on the mean): that is **input data drift** — the 2024 fuel/ENTSO-E
  inputs (the "year under surveillance") were re-ingested after the `45bfbb0` baseline was captured, so the
  same byte-identical code yields a slightly different 2024 output. Not a code regression; resolved by
  re-capturing the baseline (an F8 golden task, left to the user per commit discipline). 2019 and all
  non-dispatch artifacts still match the baseline.
- **F7 (in progress):** calibration on 2024 (spec §9) — opened with the **S1c investigation**, which
  overturned the standing "export lock" explanation of the missing FR negative tail. Five instrumented
  backtests + an input-data check established: (1) FR export ×0.4 → still 0 negatives (export is *not* the
  lock — the mean drops 46→32 but the sign never flips); (2) true REMIT availability alone → 0; (3) REMIT +
  `c_start`×10 → 0 (the LP still sheds commitment); (4) REMIT + a 40 GW C7 floor → the **first 3 negative
  hours** (mechanism found); (5) the surplus is real in the data — on the 157 observed-negative hours,
  net load (demand − must-take RES) ≈ **27 GW** vs a full-fleet band floor 0.60·61.4 ≈ **37 GW**.
  Flat-`p_minstab` probes (45/50 GW) then showed that lever is too blunt: count saturates at 9–12 (vs 65
  in-window), modulated energy *falls* (a floored fleet cannot modulate), and the FR mean collapses
  42→40 vs 56 observed (shoulder hours flooded). The correct formulation, landed as code:

  * **flex→REMIT availability coupling** — `run_backtest` forces `use_remit_nuclear_avail=True` when FLEX is
    on: the module models modulation *endogenously*, so nuclear must carry its true envelope (installed −
    REMIT outages); the rolling-max-of-output proxy pre-removes exactly the surplus FLEX exists to price.
  * **κ commitment floor** — `u ≥ κ·avail·cap` per reactor (`u_min_frac`, seed `u_commit_frac`=0.85 in
    `dispatch_flex_costs`): commitment is *scheduled* (EDF plans the campaign; day-ahead only modulates), so
    the LP may not shed the fleet — the optimizer's free-shedding escape was the fiction behind every zero.
    κ<1 keeps the observed few weekend shutdowns available. No level distortion: `p` stays free up to `u` in
    peaks, unlike the flat C7 floor. The min-down ramp (and its F5 seam row) is widened by `κ·Δavail⁺` so a
    scheduled outage *return* is never blocked by the economic recommit cap.
  * **C7 availability-clamped** (`min(p_minstab, 0.98·Σ avail·cap)`) — the regulatory floor can never exceed
    the physically-online fleet (no infeasible windows); `p_minstab` stays 0 for 2024 (the CRE minimum-
    injection mechanism is 2026+), so C7 is a projection-era lever, not the 2024 negative-former.
  * **Sweep hygiene** — `run_backtest(write_lake=False)` for calibration sweeps (never overwrite the golden
    artifact); `flex_stats` (fleet modulated energy Σ(u−p), deep-mod energy Σd) returned for the §9 level
    target.

  Toy tests prove the κ floor forces negatives *without* a demand recovery (where the C5 start cost alone
  cannot) and survives an outage-return step.

  *Calibration finding — the β ceiling.* First κ probes (0.85/0.95) delivered the §9 modulated-energy level
  (37/41 TWh-eq vs 30–33 target) and repaired the mean (48 vs the flat floor's 40), but dropped exactly the
  surplus windows as infeasible: with a *committed* fleet, sustained deep-mod turns the C3 ramp allowance
  `r_up·u − β·Σ₈d` negative, *forcing* `p` down against the C1 band floor — a "xénon death spiral" the
  physics does not contain (xénon slows the climb, at worst to zero; it never forces output down). Since
  `Σ₈d ≤ 8·(α_band−α_tech)·u`, β has a unit-free physical ceiling `r_up/(8·deep_band)` ≈ 0.09–0.11 — the
  class seeds (0.14–0.17) exceed it. The builder now clamps the LP-effective β to the ceiling (the free
  shedding of the pre-κ model is why this never bound before).

  *Calibration finding — the fleet-operating band floor.* With κ + the β clamp, modulated energy landed in
  the §9 band (31.5 TWh-eq vs 30–33) but the count stayed 0: model nuclear could still modulate **free of
  charge** from `u` down to the class band (0.60·u ≈ 23 GW), absorbing the surplus before any negative bid
  was touched — while the revealed curve measures **74 % of available capacity producing below −40** (socle
  share), i.e. reality's fleet floor on those hours was ~30 GW, not 23. Free modulation now stops at
  `alpha_band_op` (seed 0.74, workbook-overridable) — the *fleet-operating* floor, vs the per-unit mode-G
  technical band that the fleet never rides simultaneously; below it is deep-mod `d` at `c_mod`, itself
  re-anchored to the revealed **socle bid** −40 (`c_mod ≈ 45` ⇒ implied deep-mod bid `srmc − c_mod ≈ −38`;
  the seed 8 made deep-mod absurdly cheap — model nuclear politely curtailed at −1 where reality's fleet
  holds output through −40). The merchant ladder rung bids `mer_bid` = −0.01 (curtailment just *below* zero —
  the §9 shallow band (−0.01, 0] is exactly this microstructure), not 0.0.

  *Calibration finding — the §51 trigger was erasing every FR negative (the actual, historic answer to
  "pourquoi 0 heure négative modélisée").* The FR CR tranche carried `trigger=1`; the sticky
  `solve_with_triggers` fixed point therefore zeroed its floor at the FIRST negative hour of any window and
  re-solved to an exactly-0.0 price — **retroactively deleting every FR negative run**, in every variant,
  since the §51 machinery was built (this, not the export mechanism blamed by the S1c-era analysis, is why
  the pre-FLEX backtest printed 0 vs 352). Regulatorily the German N-consecutive-hours trigger does not
  exist in the French CR: the premium suspension is instantaneous per negative hour and is *already encoded*
  in `cr_bid ≈ −1`. `apply_oa_ladder` now sets `trigger=0` on the schemes it reprices (FLEX-gated; DE's
  genuine §51 dynamics untouched). **First on-target result** (κ=0.85, c_mod=45, α_op=0.74, 15-week 2024
  window): count **65 = 65 observed**, timing 69 % midday / 55 % weekend (obs-consistent), modulated energy
  31.5 TWh-eq, P95 104 vs 103.

  *Calibration finding — the fired-tranche 0.0 sink.* Even with real FR surplus and no FR trigger, min stayed
  ≈0: a **fired** §51 tranche's floor was reset to exactly 0.0, making it an *unlimited 0-priced curtailment
  sink for the whole coupled region* — exporting into it always beat curtailing at home below zero, so no
  zone could print a real negative while any fired tranche had capacity left. `solve_with_triggers` now takes
  `fired_floor` (default 0.0 → flag-off byte-identical; the FLEX path passes the ladder's `mer_bid` −0.01):
  premium gone ⇒ merchant behaviour, curtailment just below zero. Final probe (κ=0.90): count 83 vs 65
  (within ±30 %), real prints at the merchant rung.

  *Honest depth boundary.* The model's prints stop at the shallow rung: the observed **mid-band (−5…−50,
  17 of 65 h) comes from coupling with neighbours' subsidy floors when the whole region is in surplus** —
  and the model's BE/CH/ES are *not* in surplus on those hours (bisected: model BE +5 vs obs **−11**;
  their fleets carry no FLEX-style must-run rigidity and their RES potential is curtailment-censored
  observed generation). FR-side the model behaves correctly (floors at its merchant rung; deep-mod at
  `srmc−c_mod ≈ −38` stands ready behind it). Deepening FR's own ladder to fake the mid-band would
  misattribute German/Belgian floors to French CR — the depth gap is documented as neighbour-zone scope
  (F8 backlog: extend §4-style rigidities + ladders to BE/CH/ES), not compensated. **Frozen calibration:**
  `u_commit_frac`=0.90, `c_mod`=45, `alpha_band_op`=0.74, `mer_bid`=−0.01 (all measured/micro-founded:
  campaign schedule, revealed socle bid, revealed socle share, curtailment microstructure). Full-year
  validation + calibration report in progress.
- **F8 (done):** scenario horizon, documentation, tests, golden. **(1) The cross-over** (headline result,
  `FLEX_CALIBRATION_2024.md`): sampled 2028/2034/2040/2046 half-years, FLEX on, SMC level — negative count
  rises 191 → 1779 h with RES build-out while depth attenuates with the ladder's vintage expiry (min −1.0
  while CR lives → −0.01 merchant-only by 2040+). **(2) Sensitivities** (one-at-a-time, tabulated): the
  2024 tail hangs on {κ, α_band_op, c_mod, mer_bid/fired_floor} + export caps; `cr_bid`/`oa_bid`/
  `p_minstab`≤20 GW/reserves ×2 are inert. **(3) Docs**: the complete algebraic statement + the exclusions
  backlog (above), `docs/MODELLING.md` §6h (incl. the formal correction of §6d's export-lock conclusion),
  `METHODOLOGY.md` FLEX section, the `dispatch_README` workbook sheet. **(4) Walkthroughs**: two negative
  episodes + one deep-winter scarcity hour decomposed via `debug_hour` (report). **(5) Dual-quality
  hardening found in anger**: the ε tie-break extended to negative RES tranche floors; the flex path bounds
  every solve on a *fresh* HiGHS instance (the resident instance's cumulative clock made `time_limit` fire
  instantly) with an IPM+crossover rescue; a residual pathological window class (~9 % of high-RES weeks,
  C3×seam degeneracy — bisected: dropping either clears it) is skipped by the fallback, backlogged for a
  targeted seam-C3 relaxation. **(6) Golden**: flag-off byte-identical throughout (kept); the flag-on
  baseline is opt-in via `tools/golden.py capture-flexon`/`check-flexon` on its own dataset
  (`backtest_prices_flexon`).
- **Neighbour-zone extension (post-F8, 2026-07-28):** `flexibility/neighbour_nuclear.py` — pseudo-unit
  rigidities for BE/CH/ES nuclear from **measured per-zone anchors** (near-must-run fleets: κ·α_op
  0.96–0.99, socle bids −55…−70 — far stiffer than FR's load-following 0.67/−40), per-zone F5 seam state,
  §4 fossil standalone builder, DE tranche volumes year-correct from the registry. BE/CH negative counts
  move off zero for the first time (0 → double digits; one calibration hit CH 50/50 exactly). Locked by
  A/B: unit-level DE stays opt-in (2024-harmful), fired-tranche floor = the German-law 0.0. Honest
  boundary (see the calibration report): residual count errors now track the **zonal level biases**
  (over-printers = zones modelled too long; under-printers = too short) and the FR mid-band waits on
  neighbour DEPTH, capped by curtailment-censored RES potential — the rigidity layer itself is complete.
