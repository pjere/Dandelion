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
| C1 | `p ≥ α_band·u − d`, `d ≤ (α_band − α_tech)·u` — two-tier minimum (band floor + deep band) | F2 |
| C2 | `Σ_{k=0..7} d_{t−k} ≤ D_max8h`, `Σ_{t∈day} d ≤ D_max_day`, cost `c_mod·d` in objective | F2 |
| C3 | `p_t − p_{t−1} ≤ R_up·u_t − β·Σ_{k=1..8} d_{t−k}` — xénon up-ramp asymmetry | F2 |
| C4 | maneuverability {full / reduced (½ caps) / none (`p = avail·u_frozen`, must-run stretch-out)} | F3 |
| C5 | start cost `c_start·su`; min-down proxy `u_t ≤ u_{t−1} + avail·ρ_recommit` | F3 |
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
| D_max8h, D_max_day (deep-mod caps) | physics (level) / calibrated (scale) | `reactor_class` + F7 | design envelope; fit to ~30–33 TWh/yr |
| maneuverability, stretch-out profile | physics (derived) | availability_model (F1) | fuel-cycle position (scheduler / REMIT gaps) |
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
- **F2b (todo):** C2 budgets (`D_max8h`, `D_max_day`) + C3 xénon ramp + min-down persistence; thread `flex`
  through `solve_multizone`→`solve_with_triggers`→`run_backtest`/`project_year`; build the real per-reactor
  FR nuclear stack (skip `nuclear_curve` when flex on, per-reactor bids from the revealed curve, attach F1
  maneuverability); validate on one real negative week.
- F3…F8: see the phase tasks and the work plan.
