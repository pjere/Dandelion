# FLEX-F7 — calibration report, 2024 backtest (spec §9)

> The FLEX module calibrated on the full 2024 year with actual inputs (REMIT availability, observed
> commodities, flow-derived NTC). Frozen parameter set, the §9 modelled-vs-observed tables, the two
> diagnostic checks, and the honest statement of what is and is not reproduced. Companion to
> `FLEXIBILITY.md` (the living spec — see its F7 status for the investigation trail).

## Frozen calibration — every parameter measured or micro-founded, none free-fitted

| parameter | value | derivation (not a fit) |
|-----------|-------|------------------------|
| `u_commit_frac` (κ) | **0.90** | commitment is *scheduled*: the campaign keeps the available fleet committed; a few weekend shutdowns out of ~50 units ⇒ κ<1. Cross-check: κ·α_op = 0.90·0.74 = **0.67** = the observed fleet floor on the 2024 negative hours (30 GW output / 45 GW available) |
| `alpha_band_op` | **0.74** | the revealed supply curve's **socle share**: 74 % of available capacity produces below −40 €/MWh (`stacks.nuclear_curve`, measured 2019–24). Free modulation stops here; the per-unit mode-G band (0.55–0.60) is never ridden fleet-simultaneously |
| `c_mod` | **45 €/MWh** | the revealed **socle bid**: the fleet demonstrably holds output through −40 (`MUSTRUN_BID`, measured), so the implied modulation cost is `srmc − (−40) ≈ 47`; 45 retained ⇒ implied deep-mod bid ≈ −38 |
| `mer_bid` | **−0.01 €/MWh** | curtailment microstructure: merchant/post-support RES curtails just *below* zero (imbalance/shutdown micro-costs) — the §9 shallow band (−0.01, 0] is this behaviour |
| β (xénon) | clamped to `r_up/(8·deep_band)` ≈ 0.07–0.11 | physical ceiling: the C3 ramp allowance may reach zero but never negative (xénon slows the climb; it never forces output down). Class seeds 0.14–0.17 exceeded the ceiling |
| `c_start`, D-caps, ρ_recommit | class seeds unchanged | not binding at the frozen point; F8 sensitivities cover them |

Three **mechanism repairs** were prerequisites (details in FLEXIBILITY.md F7):
flex→REMIT availability coupling (the output-proxy pre-removed the surplus); the κ commitment floor (the
LP's free shedding was the fiction behind every zero); and two fixed-point semantics fixes — the FR CR
`trigger=1` erased every FR negative run retroactively (the German N-hour trigger does not exist in the
French CR; its suspension is instantaneous and already encoded in `cr_bid`), and a fired tranche's floor
of exactly 0.0 made it an unlimited 0-priced sink for the whole coupled region (now `fired_floor` =
`mer_bid`, flag-off default 0.0 unchanged).

## §9 targets — modelled vs observed (full year 2024, frozen seeds, `write_lake=False`)

| target | modelled | observed | tolerance | verdict |
|--------|----------|----------|-----------|---------|
| negative-hour count | **335** | 352 | ±30 % | **✓ (−5 %)** |
| timing: midday 11–16h share | 62 % | concentrated 11–16h | qualitative | ✓ |
| timing: weekend share | 67 % | weekend skew | qualitative | ✓ |
| timing: mean episode length | 3.6 h | ≈ 5 h | qualitative | ✓ (order right) |
| timing: spring (Mar–Jun) share | 56 % | spring skew | qualitative | ✓ |
| fleet modulated energy | **40.8 TWh** | 30–33 TWh/yr | ±15 % | **✗ +24 %** (see caveat) |
| depth: shallow (−0.05, 0] | 335 (100 %) | 98 (28 %) | bimodal | **✗** |
| depth: (−5, −0.05] | 0 | 113 | — | ✗ |
| depth: mid (−50, −5] | 0 | 125 | — | ✗ |
| depth: deep < −50 | 0 | 16 (min −87) | fat tail | ✗ |
| non-regression: winter mean | 53.5 | flag-off 55.8 | unchanged-ish | ✓ (−4 %) |
| non-regression: winter P95 | 116.4 | flag-off 110.1 | unchanged-ish | ✓ (+6 %) |
| non-regression: scarcity hours (>200) | 0 | flag-off 0 | unchanged | ✓ |
| non-regression: annual baseload | 35.6 | flag-off 37.6 | unchanged-ish | ✓ (−5 %) |

*Modulated-energy caveat:* the model measure is Σ(u−p) over committed nuclear (capacity held on but not
producing). The EDF ~30–33 TWh "énergie modulée" perimeter (load-following placements below available
power) is not exactly this; the 15-week spring window gave 32.2 TWh-eq (in-band), the full year 40.8 —
the overshoot is winter head-room the EDF measure likely does not count. Flagged as measure-definition
uncertainty, not tuned away.

## The two §9 diagnostic checks

**"Correct count but wrong depth ⇒ the bid ladder is wrong."** Applied — with a refinement the
investigation forced: the *FR* ladder is right (merchant −0.01 / CR −1 / OA at floor / nuclear deep-mod
standing at ≈ −38 behind them), and all 335 model prints sit at its first rung. The missing depth is the
**coupled region's** ladder: the observed mid-band (−5…−50, 125 h) occurs when FR's *neighbours* are
simultaneously in surplus and their subsidy floors set the coupled price (measured on the reference
hours: model BE **+5** vs observed **−11**; model ES +4 vs −0). The neighbour fleets carry no FLEX-style
must-run rigidity and their RES potential is curtailment-censored observed generation, so they absorb
FR's exports at positive prices instead of sharing the surplus. Deepening FR's own ladder would fake the
depth by misattribution — deliberately not done.

**"Correct depth but wrong count ⇒ the rigidities are wrong."** Not triggered: the count is on target,
and its formation chain is the rigidity set (κ floor → operating band → forced surplus → ladder prints),
each link verified by an instrumented experiment (FLEXIBILITY.md F7 trail).

## Reference episode (mid-April 2024, direct window solves at the frozen seeds)

| window | deepest hour | model price | obs price | fleet state (57 units) | nuclear output | week count m/o |
|--------|--------------|------------|-----------|------------------------|----------------|----------------|
| Apr 1–7 | Apr 1, 12:00 | **−0.01** | −0.0 | 0 shut · **52 at/below the operating floor** · 10.8 GW withheld | **31.2 GW** (obs ≈30) | **22 / 21** |
| Apr 8–14 | Apr 13, 10:00 | −0.00 | −20.6 | 0 shut · 55 at/below floor · 9.7 GW withheld | 24.9 GW | 9 / 31 |

The mechanism is reproduced in window 13 almost exactly: the committed fleet rides its operating floor
(52 of 57 units), ~11 GW withheld while committed (plus the κ-slack ≈4.5 GW uncommitted ⇒ order-15 GW
total modulation vs the observed "order-20 GW"), nuclear output within 1 GW of observed, and the week's
negative count within one hour. Two honest deviations: (i) "several units fully **shut**" is not
reproduced — the continuous LP spreads the κ-slack pro-rata across units instead of concentrating it in
whole-unit weekend shutdowns (a known LP-relaxation artifact; whole-unit shutdowns need integers, which
the pure-LP mandate excludes); (ii) window 14's *depth* (−20.6 observed) is the neighbour-coupling gap
documented above — its count shortfall (9 vs 31) is the same mechanism (the deepest week needs the
region-wide surplus).

## Dual-decomposition walkthroughs (F8, via the F6 `debug_hour` dump)

**Negative episode 1 — 2024-04-01 12:00, model −0.01 vs obs −0.0.** All **five FR export borders
saturated simultaneously** (FR>DE 2493/2493, FR>BE 2535/2535, FR>CH 2053/2053, FR>IT 1977/1977,
FR>ES 1884/1884 — ≈10.9 GW, the coincident caps): the export lock, visible. The committed fleet rides its
operating band — 43.3 GW committed, 31.8 GW producing, 11.4 GW withheld, 4 units deep-modulating — and the
FR dual decouples *below* every neighbour to the domestic merchant/fired floor (−0.01). `set_by_constraint`
= True: no partially-loaded domestic block carries the price; the saturated borders + the curtailment floor
do. This is the §9 shallow print, decomposed.

**Negative episode 2 — 2024-05-13 13:00, model −0.00 vs obs 0.0.** Same signature at the zero boundary
(FR>BE/CH/IT saturated plus DE's own exports saturated toward BE/CH — the region-wide surplus co-movement).

**Deep-winter scarcity — 2024-01-19 08:00, model 125.7 vs obs 108.3.** The mirror image: **imports
saturated into FR** (DE>FR 1254/1254, ES>FR 3287/3287), fleet at 64.0/64.6 GW committed (0.6 GW headroom —
the C6 reserve binding), and the price carried by a domestic partially-loaded block — the hydro water-value
tranche at 125.7 (`set_by_constraint` = False). No FLEX distortion of scarcity formation: the upward
non-regression, decomposed.

*A refinement the dump surfaced:* the deep-modulating units' `implied_bid` on the April hours is **+52**,
not −38 — the LP deep-modulates the *expensive* (top-of-revealed-curve) reactors first, because their
forgone srmc is highest. The per-reactor revealed-curve bids therefore grade the deep-mod ladder from
`srmc_top − c_mod ≈ +50` down to the socle's `7 − 45 ≈ −38`: fleet heterogeneity carries directly into the
modulation order, and the −38 rung only becomes marginal once the cheap socle itself is forced down —
consistent with how rarely reality prints below −40.

## One-at-a-time sensitivities (F8; 15-week 2024 window, frozen seeds as reference)

| case | neg | shallow | mid | deep | min | mean | P95 | mod TWh-eq |
|------|-----|---------|-----|------|-----|------|-----|-----------|
| **frozen (ref)** | 83 | 83 | 0 | 0 | −0.01 | 45.0 | 104 | 32.2 |
| c_mod 25 (−44 %) | **69** | 69 | 0 | 0 | −0.01 | 45.4 | 104 | 33.0 |
| p_minstab 20 GW | 83 | 83 | 0 | 0 | −0.01 | 45.0 | 104 | 32.2 |
| oa_bid −40 (old proxy) | 83 | 83 | 0 | 0 | −0.01 | 45.0 | 104 | 32.2 |
| cr_bid −5 | 83 | 83 | 0 | 0 | −0.01 | 45.0 | 104 | 32.2 |
| reserves ×2 | 83 | 83 | 0 | 0 | −0.01 | 45.0 | 104 | 32.2 |

**What the negative-price tail hangs on — stated explicitly.** The 2024 tail is driven by
**{`u_commit_frac` (κ), `alpha_band_op`, `c_mod`, `mer_bid`/`fired_floor`}** plus the coincident export
caps. `c_mod` is a clean monotone count lever (8 → 0 negatives, 25 → 69, 45 → 83: cheaper deep-mod lets
nuclear absorb the surplus at less-negative implied bids before the curtailment rungs are reached). The
κ progression from the F7 trail (0.85 → 65, 0.90 → 83) is the other count lever. Everything else is
**inert in 2024**: `p_minstab` up to 20 GW (dominated by the κ·α_op floor ≈ 30 GW), `oa_bid` and
`cr_bid` (never marginal — the surplus never exhausts the merchant/fired rung), and `reserves` ×2 (the
committed fleet's ~11 GW headroom dwarfs the requirement). The OA-expiry-schedule sensitivity is
projection-side by nature — covered by the cross-over run (OA share by year). The perfect identity of
the inert rows is itself a determinism check (the ε tie-break holds the vertex fixed across runs).

## The 20-year cross-over (F8 scenario check, sampled horizon, Jan–Jun windows, SMC level)

| year | §51 trig | OA % | CR % | merchant % | FR neg hours | min | mean of negatives |
|------|----------|------|------|------------|--------------|-----|-------------------|
| 2028 | 1 h | 34 | 58 | 8 | 191 | −1.0 | −0.08 |
| 2034 | 1 h | 10 | 58 | 32 | 832 | −1.0 | −0.02 |
| 2040 | 1 h | 4 | 42 | 54 | 1452 | −0.0 | −0.01 |
| 2046 | 1 h | 0 | 0 | 100 | **1779** | −0.0 | −0.01 |

The headline structural result, reproduced: the negative-hour **count rises** monotonically with RES
build-out (191 → 1779 per half-year; 41 % of hours by 2046) while the **depth attenuates** as the legacy
subsidy ladder expires — the deepest print tracks the deepest *surviving* rung (−1.0 while CR lives,
collapsing to the merchant −0.01 once OA/CR have fully rolled off by 2040+). Within the documented FR-scope
depth boundary (no neighbour mid-band), this is exactly the "more frequent, shallower" cross-over the
scheme-evolution design predicts. Caveats: SMC level (markup off), C4 maneuverability not wired in
projection, and 7 of 81 windows skipped as solver-pathological (see below) — a small undercount on the
heaviest weeks.

*Solver pathology, isolated by bisection:* one window class (~9 % of high-RES weeks) is feasible but
pathologically degenerate — removing **either** the C3 xénon rows **or** the F5 seam state clears it in
seconds; both together defeat simplex (180 s) and IPM+crossover (600 s). The flex path now bounds every
solve (fresh HiGHS instance per solve — the resident instance's cumulative clock made `time_limit` fire
instantly, poisoning whole years) and skips the pathological weeks. Backlog: drop the seam `d_hist` carry
from the C3 rows only (keep the C5/min-down/C2a seams) as the targeted fix.

## What F7 changes when the flag is on (and only then)

All F7 behaviour is gated: `flexibility.enabled: false` remains **byte-identical** (fingerprint-proven
across pre-FLEX `45bfbb0`, F5 HEAD, and the F7 tree; `fired_floor` defaults to the historic 0.0; the
golden 2024 artifact holds the flag-off full year — its only diff vs the stale baseline is the
documented input-data re-ingestion, pending a baseline re-capture).

## Deferred to F8 (each with expected sign)

- **Neighbour-zone rigidities + ladders (BE/CH/ES)** — the depth unlock; expected to move the mid/deep
  bands toward observed (deeper coupled prints), count roughly unchanged.
- **RES potential censoring** — observed generation understates potential exactly on curtailed hours;
  fixing it deepens the surplus ⇒ more/deeper negatives.
- **Episode-length dynamics** (3.6 vs ≈5 h) — longer runs come with neighbour surplus persistence.
- Scenario checks (20-year cross-over), one-at-a-time workbook sensitivities, dual-decomposition
  walkthroughs, and the flag-on golden baseline — the F8 deliverables proper.
