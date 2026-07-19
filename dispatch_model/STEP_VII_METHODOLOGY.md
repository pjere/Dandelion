# Step (vii) — SMC → day-ahead spot: the price layer

Step (vi) produces **system marginal cost** (SMC) — the dual of each zone's hourly energy balance in the
economic-dispatch LP. Step (vii) turns SMC into a **day-ahead spot forecast** and projects it out to 2046.
It has three parts, built in this order (structure first, then calibration):

1. **Structural dispatch fixes** so the SMC itself is right *in shape* (levels, spreads, and — critically —
   negative-price frequency, which a smooth markup can never manufacture after the fact).
2. **The markup** (`markup.py`): a transparent, per-zone regression of the wedge `spot − SMC` on
   *projectable structural drivers*, fitted on backtest residuals.
3. **The projection** (`rolling/projection.py`): evolve the structure to 2027–2046 (RES build-out, subsidy
   roll-off, coal phase-out, forward commodities), clear the LP, and lift SMC → spot with the fitted markup.

This note consolidates all three and states honestly what is calibrated, what is parametric, and what
remains. It supersedes the "Contract with step (vii)" stub in `METHODOLOGY.md`.

---

## 1. Structural dispatch fixes (why the SMC is right before we calibrate)

The markup can correct a *level* bias (it is a regression with an intercept and a price-proportional term),
but it **cannot invent shape** — it cannot turn a monotone-positive SMC series into one with 200 negative
hours. So every structural driver of shape had to be fixed inside the dispatch first. In order of impact on
the 2019 backtest:

| # | Fix | What it does | Effect |
|---|---|---|---|
| VII-A | **DE export sink** (`DE_REST`) | virtual NL+AT+DK+PL+CZ block on DE-LU's out-of-model borders, so DE surplus has somewhere to go | DE baseload +4.5 %→−12.6 % gap closed toward realism; enables the export-congestion mechanic |
| 64 | **Neighbour must-run floors** | seasonal CHP/heat-obligated minimum generation, **measured** from MaStR (DE lignite 0.09, gas 0.60 — not literature 0.45/0.15) | DE negative hours 0 → 665 (then bid-stack trims to realistic) |
| 63 | **Per-zone gas basis** | PSV (IT) / MIBGAS (ES) / NBP (GB) premia over TTF, not one hub | IT/ES structural +€2–5/MWh_th |
| VII-B | **IT/ES regulatory floor at 0** | pre-TIDE / pre-Dec-2023 those markets were floored at €0 | removes spurious model negatives there |
| 71 | **RES subsidy bid stack + §51 trigger** | RES bids at −(premium kept at negative prices); the EEG §51 negative-hour trigger switches the premium off (sticky fixed point) | DE negatives 665 → realistic ~145; **the** mechanism behind the negative-price *depth* |
| 74 | **Coincident NTC** | each zone's per-border p99.5 export caps scaled by a coincidence factor = p99.5(total simultaneous export)/Σ(per-border p99.5) | DE negative hours **21 → 144** (obs 210); see the trade below |

### The negatives tail and the coincident-NTC trade (#74)

Per-border effective NTCs are the p99.5 of realized flow, but those per-border peaks occur at **different
hours**, so their sum overstates a zone's achievable *simultaneous* export. That phantom headroom is exactly
what let the model clear region-wide surplus that in reality congested and priced negative. The coincidence
factor congests a zone's export directions together (DE factor ≈ 0.80, FR ≈ 0.76 across its 5 borders; ES ≈
1.0 — single in-model border, no coincidence). Result on 2019 DE-LU: **negative hours 21 → 144** against 210
observed.

This is a deliberate, documented **trade**: cutting FR's simultaneous export headroom traps French
nuclear/hydro at home, pulling FR baseload from −3.8 % to **−17 %**. We accept it because the two errors are
not equal — the recovered DE negatives are *shape* the markup cannot reproduce, while the FR level bias is
*exactly* what the markup absorbs (FR mean markup **+€5.6** → 32.8 + 5.6 ≈ 38.4 vs 39.5 observed). The
markup is fitted on the *same* coincident-NTC SMC, so the pipeline is self-consistent. Residual gap (144 vs
210, and BE still at 0 vs 71 observed) is honest: BE has no RES-bid/must-run tranches yet, and fully closing
DE's tail likely needs zone-splitting (DE_REST is a single aggregate). Tracked, not hidden.

### 2019 backtest, post-structural, pre-markup (the markup's training signal)

```
zone      model  obs   baseload   corr   neg_model  neg_obs
FR        32.8   39.5   -17.0 %   0.75      0         27
DE_LU     33.9   37.7   -10.0 %   0.74    144        210
BE        33.1   39.3   -15.8 %   0.57      0         71
CH        42.6   40.9    +4.0 %   0.65      0         17
IT_NORTH  45.4   51.3   -11.5 %   0.40      0          0
ES        35.3   47.7   -26.0 %   0.61      0          0
```

The parallel negative level gaps (SMC below spot in 5/6 zones) and the missing peak spikes (P95 negative
everywhere) are precisely the wedge the markup is built to close.

---

## 2. The markup (`markup.py`)

**`spot = SMC + markup(drivers)`**, a per-zone ordinary least squares of the wedge `observed − SMC` on
structural drivers, fitted on backtest residuals.

### Why regularized regression on structural drivers, not a black-box learner

The markup has to extrapolate to **2040** — a regime with far more RES, higher tightness, and higher price
levels than any training year. A gradient-boosted / neural wedge would extrapolate unpredictably outside its
training envelope; a transparent linear model on economically-meaningful drivers degrades **gracefully**. And
the drivers are deliberately restricted to quantities the projection engine also produces — **no calendar-year
effects** (a "2022 dummy" cannot be evaluated in 2040). Fitted per zone: each market's microstructure differs.

Making "graceful" actually true took two measures — a first plain-OLS cut did **not** degrade gracefully, it
produced absurd **−€68** markups for IT-North in 2030 (SMC €126 → "spot" €58):

- **Economic sign constraints** (`_SIGN_LB`, bounded least squares): the wedge must be non-decreasing in the
  price level (`smc ≥ 0`) and in tightness (`tight`, `peak_kink ≥ 0`). This is what makes the model
  projectable, and it earns its keep — see the artifact below.
- **Envelope clamping**: the structural ratios `tight` and `res_share` are clamped to their training
  [p1, p99] before prediction, so a 2040 RES share far beyond anything in 2019 holds the wedge *flat*
  rather than letting a linear term run away.
- Ridge (penalty `alpha_frac·n` on standardized drivers) is retained for numerical conditioning, but a sweep
  over `alpha_frac` ∈ [1e-4, 0.1] moved every diagnostic by <0.01: **the constraints, not the ridge, do the
  work.** Do not tune α expecting an effect.

#### The `smc` coefficient artifact — why the "better" unconstrained fit was a mirage

Unconstrained, the fit looked far better (RMSE roughly *halved*, FR R² 0.85). It achieved that with an `smc`
coefficient of **−0.83**. That is not economics, it is mechanical: regressing the wedge `(observed − smc)` on
`smc` has slope `d(observed)/d(smc) − 1`, so an SMC that explains spot only weakly (slope ≈ 0.2) mechanically
yields ≈ **−0.8**. The regression was therefore **shrinking an over-volatile SMC back toward the 2019 mean**
(`spot ≈ 0.17·smc + …`) rather than learning an additive wedge. In-sample that is a *legitimate* variance
reduction; out to 2040 it is meaningless — shrinking toward a 2019-calibrated mean — and it is precisely what
generated the negative markups. Forbidding it (`smc ≥ 0`) costs ~40 % of the in-sample R² and reveals the
**honest** fit quality below. We take the honest, projectable model.

### Drivers (all projectable)

| driver | captures | projectable because |
|---|---|---|
| `smc` (level) | proportional bidding markup, level-scaling of the wedge | the projection produces SMC |
| `tight = (demand − musttake_res)/firm_cap` | system tightness | projected demand/RES vs scaled firm stack |
| `tight²`, `relu(tight−0.9)` | convex scarcity rent as the system approaches firm limits | same |
| `res_share = musttake_res/demand` | downward surplus decoupling | projected RES |
| hour + season (sin/cos harmonics) | unit-commitment / ramping shape | recurring, not a year |

`firm_cap` = dispatchable capacity (nuclear, gas, coal, lignite, oil, biomass, reservoir hydro, geothermal);
must-take RES/ROR/PSP and imports/DSR are excluded from the denominator by construction.

### Fit diagnostics (2019 — the validated dispatch regime; sign-constrained = the shipped model)

The constrained wedge **improves on raw SMC in every zone**, but modestly — and this is the real number, not
the flattering unconstrained one:

```
zone       n     rmse_smc  rmse_spot   R²_spot   mean_markup   wedge @ 2040-probe
FR       8735      12.25      9.60      0.533       +6.7            +9.8
DE_LU    8735      11.26      9.46      0.628       +3.8            +4.3
BE       8735      16.65     14.77      0.328       +6.2            +5.5
CH       8735       9.89      9.28      0.435       −1.6            −4.0
ES       8735      15.58      8.29      0.420      +12.4           +10.4   (largest — matches −26 % SMC gap)
IT_NORTH 8735      18.75     17.05     −0.746       +5.9            −3.5
```

Read this honestly:
- **RMSE falls in all six zones**, most for ES (15.6→8.3) and FR (12.3→9.6); the mean markups carry the right
  sign (positive where SMC under-priced, **negative for CH** which SMC over-priced).
- **R² is modest (0.33–0.63), and IT-North is negative (−0.75)** — its wedge is worse than a constant. That is
  not a markup bug: IT-North's SMC↔spot correlation is only **0.40** (the worst zone), so no wedge on these
  drivers can rescue it. IT-North needs dispatch work (PSV basis, Italian scarcity/capacity premium), not
  calibration.
- The **2040-probe** column applies the wedge to a deliberately out-of-envelope regime (SMC €250, RES share
  0.42, low tightness). All wedges stay small and bounded — the projectability guard holds, and is pinned by
  `test_markup_does_not_collapse_when_extrapolated_to_a_2040_like_regime`.

The fitted model is serialized to `reports/markup_model.json` (`{zone: {feature: coef}}` + diagnostics) and
loaded by the projection. `apply_markup` clips the result to `[floor, VoLL]` and falls back to clipped SMC
for any zone the fit never saw.

> **Multi-year panel (#66) — DONE, quality-gated.** The markup is now fit on a genuine three-regime panel:
> **2019** (normal, ~€40), **2022** (gas crisis, €200–500), **2023** (~€90). Getting there took a real
> data-completeness fix and a calibration gate:
> - **DE-LU islanding fixed.** DE-LU collapsed (−37 %) in 2022/23 not because of generation but because the
>   `DE_REST` export-sink constituents (NL/AT/DK/PL/CZ) had no **load** in the DB — an empty net-load yields a
>   degenerate LP time coord. Ingesting their load + generation + flows for 2022–23 restores DE-LU to −7.7 %
>   (2022) / −15.9 % (2023). A guard drops any zone whose net-load is missing rather than crashing.
> - **Quality gate in `build_panel`.** A zone-year the dispatch prices badly is a *failed dispatch*, not a
>   wedge to learn, so it is dropped on either symptom: gross level error (median ratio outside [1/1.8, 1.8])
>   or wrong shape (SMC↔spot corr < 0.2). This drops **CH/IT-North 2022** (drought/nuclear-crisis year — level
>   ≈ OK but correlation ≈ 0) while keeping FR/DE-LU/BE/ES's crisis-price signal.
>
> Balanced fit (2019+2022+2023, 139 616 rows): RMSE falls in all six zones — **FR 96.6→75.2, DE-LU 86.5→71.3,
> BE 79.4→66.7** — with R² 0.54–0.74 for five zones. **IT-North stays R² −1.1** (its SMC↔spot correlation is
> ~0.37 even in a normal year — an IT *dispatch* problem: PSV basis, Italian scarcity/capacity premium — not a
> calibration one). The markup now spans €40→€500, so the `smc`-level slope is trained on crisis prices and
> extrapolates to the 2040 regime — the capability #67's 2019-only fit lacked. Remaining: CH/IT crisis-year
> over-scarcity (dropped, not fixed) needs per-year NTCs + drought hydro-budget validation.

---

## 3. The projection (`rolling/projection.py`)

Clears **future** years from a reference historical year's hourly weather shape, evolving the structure:

- **RES subsidy bid stack evolves from the registry** (`scheme_evolution.scheme_shares`): each vintage rolls
  off support 20 y after commissioning → merchant; new build enters under the prevailing scheme; the §51
  trigger tightens 6h→1h on schedule. So the trajectory shows RES growth pushing *more* surplus while the
  roll-off makes the resulting negatives *shallower and shorter* — derived, not fitted.
- **Structure**: coal/lignite phase down but the retired firm MW is **replaced 1:1 with CCGT** (adequacy —
  without this the projection manufactures false VoLL); demand + RES scale; forward commodities per year.
- **Markup applied**: SMC → spot with the fitted model, using the *projected* drivers.

Verified end-to-end (3-week windows, markup loaded from `reports/markup_model.json`) — SMC → spot:

```
            2019 (trigger 6h)        2030 (1h)            2040 (1h)
zone        SMC  → spot   neg      SMC  → spot          SMC  → spot
FR         43.0 → 52.4     0       99.0 → 109.7        102.1 → 114.1
DE_LU      32.4 → 39.3    11       55.9 →  62.3         39.6 →  45.4
BE         43.2 → 52.4     0      103.0 → 111.8        176.5 → 184.6
CH         52.9 → 51.1     0      125.9 → 125.6        138.2 → 139.1
IT_NORTH   54.7 → 56.9     0      126.5 → 128.7        138.6 → 140.7
ES         42.9 → 56.8     0       95.4 → 109.0         81.9 →  94.5
DE_REST    36.0 → 36.0     9       91.8 →  91.8         86.8 →  86.8   (no observed spot → no wedge, by design)
```

The wedge stays a modest, positive, bounded uplift at every horizon, and negative hours survive the markup
(DE-LU 2019: 11 hours, mean −€16.6) rather than being smoothed away.

### Weather-coherent projection (#77) — built, FR exact + neighbours reduced-form

The projection no longer *has* to hold the 2019 weather shape fixed. `weather_shapes.py` + the `project_year(
weather_shapes=…)` hook let a projected year run on a **re-drawn weathergen shape**:
- **FR is exact.** `fr_draw` runs the demand model (step iii) and RES model (step iv) on one weathergen
  realization — both consume the *same* cube, so FR demand and RES are weather-coherent — and assembles the
  net load. (2040 FR from a draw: 68.9 GW mean, 102 GW peak, 31 GW must-take RES.)
- **Neighbours are reduced-form.** They have no demand/RES models, so `NeighbourWeatherModel` fits, per zone,
  load ↔ FR national temperature (HDD/CDD + calendar) and RES ↔ the FR national RES-CF shape × the zone's
  capacity — justified by the strong spatial correlation of European weather. Weather-*coherent* (same FR
  draw) but not station-resolved. Levels sit at the right magnitude (BE 10, CH 8, ES 29, DE-LU 59 GW).
- `all_weather_shapes(year, realization, nb_model)` assembles the `{zone: df}` payload; the hook re-indexes
  onto the reference calendar for windowing and borrows the ref-year nuclear/reservoir shapes (maintenance-
  scheduled, not weather-driven). **Validated end-to-end:** a 2040 draw flows all the way to zonal prices and
  the weather draw visibly moves them (FR €229 fixed → €1371 on a cold-calm draw).

One honest caveat remains: the neighbour models are reduced-form (a full build extends weathergen to
neighbour stations + fits per-zone demand/RES models).

### 2040 winter adequacy — the flexibility fleet (#83)

Running #77 end-to-end first exposed an adequacy hole: the 2040 reserve margins were **negative** in most
zones (BE −32 %, IT-North −39 %, DE-LU −13 %) because TYNDP retires firm thermal while demand grows ×1.22 —
and the dispatch had **no 2040 flexibility** (batteries, demand-response, H2 peakers), the capacity that
actually keeps a decarbonised system adequate. So a low-RES cold snap cleared at VoLL (BE €2800+, CH €3000).
The fix (`cap_flex_gw` in `dispatch_tyndp`, injected by `_append_flex`, priced at its €180 VOM) adds that
fleet as a peaking backstop. Margins recover to **+11 – +26 %** and 2040 winter prices fall to a plausible
**€80 – €174** (tight zones clear at the ~€180 flex ceiling, not VoLL; high-RES zones — DE-LU, FR — show
negatives). The flex trajectory is starter data in the tab, editable from the TYNDP storage/DR/peaker scenario.

### Three honest gaps in projection realism (historical framing — see the two above for current state)

1. **Weather shape is held fixed at the reference year** (2019) rather than re-drawn from weathergen. The
   *structure* evolves correctly, but every projected year sees 2019's weather. A weather-coherent projection
   would replace the reference net-load shapes with **weathergen draws** pushed through steps (iii)/(iv)/(v)
   for each neighbour zone. This is the single largest remaining build (≈ re-running the FR demand/RES models
   for six neighbour zones on generated weather) and is scoped, not started.
2. **Neighbour demand/RES capacity path — now TYNDP-grounded (#76, done).** The flat per-tech CAGR is
   replaced, where available, by TYNDP trajectories: the `dispatch_tyndp` workbook tab holds per-zone anchor
   values for demand and per-tech installed capacity (National-Trends-style starter values, editable from
   the TYNDP portal), and `tyndp.py` interpolates them to any year and applies target/ref **factors** to
   demand, RES volume, and firm-capacity stacks (`_scale_stack(cap_factors=…)`). Missing zone/variable →
   CAGR fallback, so the tab fills incrementally. Verified: FR 2040 vs 2019 = demand ×1.22, RES ×3.51,
   nuclear ×0.90; DE lignite ×0.07. The still-parametric piece is the *hourly weather shape* (item 1).

3. **The wedge is trained on one year, so its long-horizon accuracy is unproven.** 2019 cannot pin down the
   2040 driver *combination* (high price **and** low tightness — a pairing no single historical year
   contains). The sign constraints keep the extrapolation *bounded and sane*, but bounded is not the same as
   *right*. Only the multi-regime panel (#66 above) can actually calibrate the wedge at crisis price levels —
   which is why #66 is a **prerequisite for trustworthy long-horizon spot**, not a nice-to-have.

Until those land, the trajectories are **structure-evolved spot forecasts on 2019 weather**, not
weather-ensemble forecasts. The 20-year run is directionally sound (FR 44→102→104→96 €/MWh; negatives roll
from −13.8 in 2019 to ≈0 by 2046 as the §51 trigger tightens) but should be read as a central path, not a
distribution — the distribution comes from the weathergen ensemble once item 1 is built.

### Stochastic neighbour availability (#80)

The neighbour blocks size firm capacity as p99-of-observed-generation — an availability proxy for the
*central* level, but with **no per-draw variability** (a crisis year like 2022 where a neighbour thermal
fleet is markedly less available). `neighbour_availability.py` closes this: REMIT
(`zone_availability_stats`, backfilled per zone) gives each tech's mean and across-year std of annual
availability (e.g. FR gas 0.91 ± 0.01, hydro-PSP 0.84 ± 0.03, nuclear ~0.72 with the crisis spread); a
**mean-preserving multiplier** (draw ÷ mean ≈ 1, so it does *not* double-count the p99 proxy) derates
neighbour firm capacity per Monte-Carlo draw. Wired opt-in: `project_year(..., avail_rng=rng)` with
`_preload(..., avail_years=[…])`. Retiring fleets whose REMIT "availability" is a closure artefact
(FR hard coal 2019-24 → 0.13) are dropped by a 0.4 floor. **Exercised only once the projection gains a
Monte-Carlo draw loop** — the same missing piece as the weathergen ensemble; today's central path
(`avail_rng=None`) is unchanged.

## Status summary — what is and isn't done

| item | state |
|---|---|
| Structural dispatch fixes (gas basis, must-run, DE sink, IT/ES floor, RES bid stack) | **done**, quantified above |
| #74 coincident NTC (negatives calibration) | **done** — DE 21→144 h vs 210 obs; residual gap documented |
| #67 markup fitted + wired into projection | **done** — projectable (sign-constrained); RMSE reduced in all 6 zones; IT-North poor |
| #66 multi-year training panel | **done** — 3-regime fit (2019+2022+2023) after the DE-LU-load fix + `build_panel` quality gate; markup now spans €40→€500 |
| Projection from TYNDP capacities | **not started** — tractable (workbook tab) |
| Projection from weathergen draws | **not started** — largest remaining build (steps iii/iv/v for 6 neighbour zones) |
| #80 stochastic neighbour availability | **done** — REMIT-calibrated mean-preserving multiplier, wired opt-in; needs the MC-draw loop to exercise |

---

## Performance — the solver backend (`lp/highs_solver.py`)

The window LP is solved thousands of times (21 years × 52 weeks × up to 3 §51 fixed-point iterations).
Profiling the 20-year projection showed **~90 % of the wall-clock was linopy's model *construction*** —
`xarray` index-alignment/merge on every `+`/`*`/`.sum()`, rebuilt from scratch for every solve — while the
HiGHS solve itself was milliseconds. `lp/multi_zone.py` now defaults (`_BACKEND = "highs"`) to
`lp/highs_solver.solve_multizone_highs`, which assembles the **identical** LP directly as a sparse ±1
column matrix (every coefficient is ±1 — a pure balance/flow network) and hands it to HiGHS once. The
linopy construction is retained as a cross-check backend.

**Byte-identical**: the 2019 backtest prices are unchanged to machine precision (max abs diff ~1e-15,
0 hours differing > 1e-6) — the golden artifact is untouched. The duals that define each zone's price come
straight from the HiGHS row duals (`price = +row_dual` of the energy balance).

Secondary result-neutral wins: the per-year plant-registry read is hoisted into `_preload`
(`scheme_shares(..., reg=)`); one resident `Highs()` instance is reused across solves (avoids the ~85 ms
constructor each time); the flows long-frame is built directly (the per-solve `melt` was a hotspot); and
`stacks.fr_stack.srmc` is vectorised (was a per-unit `itertuples`). **Net: a 20-year projection's solve
loop went from ~1 h (linopy) to ~11 min (~32 s/year × 21) — ≈5×** — with a one-time ~250 s preload
(amortised across Monte-Carlo draws, which reuse the same reference).

### Parallel Monte-Carlo (`rolling/montecarlo.py`)

A single trajectory is ~11 min, so a large weathergen ensemble is run **across CPU cores** — the draws are
embarrassingly parallel and each stays exact. `run_ensemble(config_path, years, draws, …)` distributes
draws over a `ProcessPoolExecutor`; each worker does the deterministic ~250 s preload **once** and reuses
it for every draw it handles. Two invariants make a parallel run **byte-identical to the serial one**: the
preload is draw-independent, and every draw's randomness comes from `powersim_core.rng.draw_rng(seed,
draw)` — a `SeedSequence` child keyed by the draw id, independent of process and order. Validated on real
data: 3 draws (2030) with the #80 REMIT-availability spread give **max abs diff 0.0** between the serial
and 2-worker runs, while the draws genuinely differ (cross-draw mean-price spread ≈3.9 €/MWh; BE
121.5–125.4, CH 119.3–121.5, …). `ensemble_stats` returns the cross-draw P5/P50/P95 per (year, zone). The
stochastic source is pluggable — #80 availability (`avail_years`) and/or the #77 weather-shape provider.

Reaching *seconds per trajectory* (rather than per ensemble) would additionally need the **hourly
decomposition** — the LP is separable by hour except for the weekly hydro-budget coupling (price hydro at
its water value and each hour is an independent tiny dispatch) — which changes the solution method and so
needs its own accuracy validation at degenerate price kinks. The parallel harness above is the
accuracy-preserving route and is done.

## Contract & provenance

- **Input**: step (vi) SMC (LP zonal duals). **Output**: hourly zonal day-ahead spot, historical (backtest)
  and projected (2027–2046).
- **Calibrated**: the markup (`reports/markup_model.json`), on backtest residuals.
- **Derived, not fitted**: the RES subsidy scheme evolution (from the plant registry + statutory support
  terms + the §51 schedule).
- **Parametric / documented-simplification**: reference-year weather shape; neighbour CAGR scaling (pending
  TYNDP + weathergen).
- Registry-driven inputs live in the reference layer (`data/lake/reference/plant_registry`, ADR-7); scenario
  overrides in `scenarios.xlsx`. See `RES_BIDDING_DESIGN.md` for the subsidy-bid derivation and `ADR.md`.
