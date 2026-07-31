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

**Refresh with storage on** (same design, PSP measured envelopes + BESS trajectory ×1→×10 wired in
projection — see the storage section of FLEXIBILITY.md):

| year | OA % | CR % | merchant % | FR neg hours | depth | min | P95 | max |
|------|------|------|------------|--------------|-------|-----|-----|-----|
| 2028 | 34 | 58 | 8 | 61 | all (−5,0) | −0.0 | 106 | 129 |
| 2034 | 10 | 58 | 32 | 675 | all (−5,0) | −0.0 | 116 | 144 |
| 2040 | 4 | 42 | 54 | 1328 | all (−5,0) | −0.0 | 136 | 180 |
| 2046 | 0 | 0 | 100 | **1691** | all (−5,0) | −0.0 | 180 | 202 |

Three results. (i) The cross-over **survives storage**: the count still rises steeply (61 → 1691,
ending near the storage-off 1779) — RES build-out dominates the storage trajectory at the horizon.
(ii) Storage bites hardest where surpluses are thin: 2028 drops 191 → 61 (−68 %) while 2046 barely
moves (−5 %) — the same thin-tail/fat-tail asymmetry as the 2024 backtest gate. The composite shape is
a genuine *dip-then-explosion*: near-term BESS build-out temporarily pushes negative-hour counts below
the 2024 observed level before RES growth overwhelms it. (iii) The scarcity caps generalize: max
129–202 €/MWh across the horizon, no 300/4000 spike prints anywhere. One caveat hardens: depth
attenuation is now *total* (every negative hour in (−5,0), mean ≈ −0.01) — storage absorbs the marginal
surplus before any deep floor can print, so deep negatives at the horizon require surpluses beyond
storage + rigidity absorption simultaneously, which these BESS trajectories preclude. All 26 weeks
solved in every year (seam-attempt failures recovered by the cold fallback; no windows dropped).

*Solver pathology, isolated by bisection:* one window class (~9 % of high-RES weeks) is feasible but
pathologically degenerate — removing **either** the C3 xénon rows **or** the F5 seam state clears it in
seconds; both together defeat simplex (180 s) and IPM+crossover (600 s). The flex path now bounds every
solve (fresh HiGHS instance per solve — the resident instance's cumulative clock made `time_limit` fire
instantly, poisoning whole years) and skips the pathological weeks. Backlog: drop the seam `d_hist` carry
from the C3 rows only (keep the C5/min-down/C2a seams) as the targeted fix.

## Neighbour-zone extension (post-F8): pseudo-unit rigidities for BE/CH/ES(/DE)

**Anchors, measured** (`scratchpad/nz_anchors.py`, the FR method — socle share / p5 bid / fleet floor on
observed-negative hours, availability = installed − REMIT): the neighbour fleets are far more rigid than
FR's — BE 0.96–0.97 / −52…−60 / 0.98; CH 0.98–0.99 / −64…−75 / 0.98–1.00; ES floor 0.98 but bid depth
**market-censored** at −1.9 (negatives legal only since 2023-12) → BE/CH value borrowed; DE 2019 ≈ 1.0 /
−67. FR remains the only load-following fleet (0.74 / −40 / 0.67). *Update 2026-07-29 — the 2025 holdout
lifted the ES censoring (544 observed negative hours): measured socle 0.93 / p5 bid −9.6 / floor 0.96.
The borrowed −60 was 6× too deep — the Spanish fleet yields at ~−10 (right-censored at the market's own
−15 floor); `_ANCHORS["ES"].socle_bid` re-set to −10. BE/CH confirmed out-of-sample (p5 −56.4 / −61.2 vs
frozen −55 / −70). A/B on the full 2025 year: byte-identical output — with only 8 shallow model-ES
negative hours the socle is never ridden, so the bid depth is inert until the ES surplus/level bias is
fixed; the correction is preventive (a 6×-too-deep bid would print phantom −60s the moment surpluses
fatten).*

*ES surplus decomposition (2026-07-30, `scratchpad/es_surplus_decompose.py`, full-year 2025 diagnose):
on the 556 obs-negative hours the model is NOT short of surplus — it prices 0.0 across the whole
distribution (p10=p50=p90) with the ES `merchant` RES tranche marginal on 526 h (nuclear pseudo-unit
30 h; gas/hydro only on obs-positive hours). Gross surplus before imports exists on just 153/544 h;
the boundary is reached via the nuclear floor (4.8 GW) + French imports (FR>ES binding 40 %). The
8-vs-556 count gap is therefore the **workbook 0.0 merchant-floor convention** — the same boundary
artifact noted for NL — against genuinely shallow Spanish negatives (obs mean −2.1, p5 −9.2). Fix
path: measured tranche floors from the 2025 revealed curtailment curve (a −1…−2 merchant floor alone
flips ~526 h ⇒ count ~534/556 with roughly correct depth); queued with the per-zone
curtailment-response measurement.* Implementation:
`flexibility/neighbour_nuclear.py` — ~1 GW pseudo-unit split of each zone's nuclear block, κ floor +
operating band + budgets + clamped xénon from the zone anchors; §4 fossil available standalone
(`build_fossil_flex_spec`); per-zone F5 seam state in the backtest loop. No C6/C7 (French regime), no C4
(no per-unit calendars abroad).

**Four A/B probes** (15-week 2024 window, negative counts model vs obs):

| zone | pre-ext | +pseudo+unitDE+fired−.01 | +blockDE | +fired 0.0 | +DE 2024 volumes | obs |
|------|--------|--------|--------|--------|--------|-----|
| FR | 83 | 59 | 136 | 107 | 102 | 65 |
| BE | 0 | 5 | 67 | 37 | **19** | 55 |
| CH | 0 | 5 | 50 | 25 | **14** | 50 |
| ES | 0 | 0 | 0 | 0 | 0 | 62 |
| DE_LU | — | 11 | 545 | 303 | **149** | 70 |
| NL | 0 | 0 | 22 | 14 | 8 | 70 |

Locked decisions from the A/Bs: (1) **unit-level DE reverted to opt-in** — the MaStR stack over-prices
2024 DE (mean 79 vs 65) and drains the regional surplus (#73 validated it on 2019 only); (2)
**fired-tranche floor back to the German-law 0.0** — fired hours clear AT zero in reality; the −0.01
variant mass-printed phantom counts (DE 545). BE/CH counts sit between the two conventions (their prints
are partly coupling-level at the region's fired tick — EPEX granularity mixes 0.00 and −0.01); (3) **DE
tranche volumes year-correct from the registry** (fit 0.30→0.18, merchant 0.10→0.20, §51 trigger 6→4 h;
DE 303→149) — **DE only**: the BE/CH/ES cohort registry is degenerate single-scheme and `scheme_shares`
would bolt a German trigger onto paid-regardless certificate schemes that have none (static tab kept).

**Where this leaves the region — the honest boundary.** The residual count errors now *track the zonal
level biases*: the over-printers (FR −11, DE −13 vs obs mean — modelled too long) and the under-printers
(CH +6, ES +5, NL +22 — modelled too short) are exactly the pre-existing baseload biases, not the
rigidity machinery. FR's mid-band is still empty because model BE/CH never print DEEP (min −0.0 vs obs
−55/−91): their deep floors (−80 certificates, −50 KEV) never become marginal while their surplus is
capped by **curtailment-censored RES potential** and conservative capacity sizing. The rigidity layer is
complete; the next binding constraints are the zonal level biases and RES-potential reconstruction.
FR modulated energy unchanged at 33.3 TWh-eq (in band).

## RES-potential reconstruction (v1: solar, neighbours — measured, kept, and honestly bounded)

Must-take RES = observed generation = **post-curtailment**, understating the surplus exactly on the hours
that price negative. Measured (`scratchpad/res_censoring.py`, same-hour ±7-day envelope, 2024): the dip
concentrates on observed-negative hours for **solar in every zone** (dip@neg/dip@pos 2.0–4.9; DE 2.8 TWh
censored on negative hours, FR 1.6, ES 1.1) but **not for wind** (0.6–1.0 — calm weather correlates with
positive prices; the envelope cannot separate curtailment from weather, so wind is excluded even though
real). `flexibility/res_potential.solar_uplift`: price-unconditioned per hour
(`max(0, dip − noise[zone,hod])`; prices enter only the per-(zone, hour-of-day) noise constant — the
documented leakage boundary), applied flex-gated to neighbour zones (FR excluded: already over-counting).

Effect (probe E vs D): counts +3 BE / +4 CH / +31 DE (+18 FR via coupling), BE/CH means pulled to within
±3 €/MWh of observed (BE −2.3, CH +3.0) — **but no mid-band depth anywhere**: the ~1 GW noise-trimmed
uplift cannot punch zone surpluses past the shallow rungs into the deep floors. Verdict: kept as a correct
modest improvement; the depth unlock is now conclusively the **zonal level-bias workstream** (stack
sizing / per-zone NTC — under-printers ES/NL/CH are modelled short, over-printers FR/DE long), not input
censoring.

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

## Out-of-sample 2025 (first true holdout — the record negative-price year; run 2026-07-29)

Full-year 2025 backtest, calibration **frozen on 2024** (κ 0.90, c_mod 45, α_op 0.74, mer_bid −0.01 —
in-code defaults, nothing refit), storage off (frozen-config parity), 2025 commodity anchors from public
annual averages (TTF 36 / EUA 72 / API2 11.5 / Brent 69), all-zone 2025 ENTSO-E inputs (post
truncated-chunk repair — see the ingest guard in `pricemodeling/entsoe/series.py::_do`). Observed side
is hourly-mean of native prints (15-min MTUs live from Oct 2025).

| zone | model neg | obs neg | model min | obs min | model mean | obs mean | model >200 | obs >200 |
|------|-----------|---------|-----------|---------|------------|----------|------------|----------|
| FR | 884 | 510 | −1.0 | −118.0 | 45.2 | 61.0 | 0 | 31 |
| DE_LU | 235 | 573 | −10.0 | −250.3 | 68.4 | 89.4 | 0 | 162 |
| BE | 274 | 516 | −1.0 | −462.3 | 58.1 | 82.6 | 0 | 74 |
| CH | **347** | **303** | −10.0 | −262.2 | 74.2 | 101.8 | 20 | 52 |
| ES | 8 | 556 | −0.0 | −15.0 | 50.7 | 65.2 | 0 | 20 |
| NL | 6 | 581 | −1.0 | −350.0 | **89.6** | **86.9** | 0 | 127 |
| IT_NORTH | 36 | 0 | −1.0 | 0.0 | 98.1 | 115.9 | 51 | 43 |

FR depth: model 854/30/0/0 across (−0.05,0]/(−5,−0.05]/(−50,−5]/<−50 vs observed 254/148/91/17.
FR timing: midday 57 % (obs 63), weekend 57 % (obs 49), mean episode 4.5 h (obs 5.0),
spring share 42 % (obs 71). Modulated energy 42.2 TWh.

**Honest verdict: the frozen calibration does not transfer as-is.** The 2024 gate result (335/352,
−5 %) does not carry to 2025: FR **over-prints count** (+73 %) while still missing *all* depth
(min −1.0 vs −118 observed), and the catalogued level-bias zones fail dramatically out-of-sample —
ES 8/556 and NL 6/581 (the too-short stacks that under-printed mildly in 2024 miss an order of
magnitude in 2025's fatter-surplus regime), DE −59 % (the measured 2024 must-run floors do not
describe 2025), BE −47 %. What *does* transfer: CH count (+15 %, inside the ±30 % band — the only
zone passing), the NL mean level (86.9 obs / 89.6 model — the capacity-fallback + tranche work closed
the level gap), FR episode length (4.5 vs 5.0 h) and midday share. Scarcity is missed everywhere
(obs 20–162 h >200 €/MWh per zone; model ≈0 outside CH/IT) and means run 14–28 € low — consistent
with the flat annual commodity anchor missing 2025's front-loaded gas curve, and with the absent
scarcity tail. Timing shape degrades too: only 42 % of model negatives fall in Mar–Jun vs 71 %
observed — the surplus count inflates outside spring.

**Reading.** The out-of-sample test did exactly its job: every weakness already catalogued in-sample
(zonal level biases, censored RES potential, no mid-band depth, no neighbour ladders at depth) is
re-exposed at 2025 amplitude, *plus* one new signal — the FR shallow-tail mechanism **over-fires**
when surpluses fatten while depth stays capped, i.e. the count/depth trade-off is structural, not a
2024 tuning artifact. Priorities the holdout confirms: (1) the zonal level-bias campaign (ES/NL/DE
stacks) is the binding constraint, not FR parameters; (2) depth needs the deep rungs (censored wind,
neighbour mid-band ladders) before any FR count retune is meaningful; (3) within-year commodity
shape (monthly historical prices) is now measurable as a 14–28 € mean error. A κ/α_op refit on 2025
is *not* warranted until (1)–(2) land — refitting frozen parameters against a structurally biased
envelope would launder stack errors into behavioural ones.

## Curtailment-response measurement, 2025 negative hours (the revealed RES ladder, per zone)

`scratchpad/curtailment_response_2025.py` — per price-depth band: print mass, solar yield vs the
±7-day same-hour envelope (F8-validated for solar), wind response vs its same-window median
(slope-only), and the **still-producing volume at depth** (no potential estimate needed: production AT
−150/−500 *is* the revealed price-insensitive stock). FR included as method reference — its known
ladder reproduces (p50 −0.1 ≈ the merchant −0.01 rung; 11–14 GW OA-exempt still on at −50…−150).

Two regimes, measured:

**Shallow-market zones (ES).** All 544 prints between −15 and 0 (p50 −1.0); solar yield ≈ baseline
even at depth (0.79/0.72/0.68 vs ref 0.72 — no measurable response in the observable range). The
ladder is {merchant ≈ −1 : 370 h, subsidized ≈ −5…−10 : 174 h}. With the decomposition result (model
sits at the 0.0 boundary on 526 h), the workbook fix is floor values, not volumes.

**Deep-ladder zones (DE/BE, NL intermediate).**
- **DE: solar yield RISES with depth (0.76→0.86, ref 0.77) — ~44 GW of German solar still producing
  at ≤−150** (the rooftop/exempt-vintage stock, measured directly); wind is the responsive half
  (resp 1.4 shallow → 0.25 at ≤−150, only 2–4 GW left). German deep prints are exempt-solar days
  after responsive wind has already left the market. The model's DE tranches must carry tens of GW at
  deep floors; its current under-print (235/573, min −10) lacks exactly this block.
- **BE: solar rides through everything (yield 0.96 at ≤−150, 6.9 GW on at −462)**; offshore
  green-certificate wind holds ABOVE its median through −150 (resp 1.15) and halves only below. The
  −462 minimum is this stock's bid. BE currently has no scheme ladder in the workbook at all.
- **NL: metered solar responds strongly (0.50→0.15 vs ref 0.72) but is a ~0.2 GW utility sliver —
  the ~25 GW salderen/rooftop fleet is behind-the-meter, invisible in ENTSO-E generation** (it nets
  demand; consistent with modelling it as a demand-side −500 tranche). SDE wind/solar rung validated:
  106 h mass in [−50,−10) matches the SDE −20 floor; deep-exempt wind ~0.6–0.9 GW at ≤−50.

Workbook implication set (measured, not yet applied): ES merchant floor 0.0 → −1 with a −5…−10
subsidized rung (predicted count 8 → ~530/556); NL merchant floor → −2; DE deep-exempt tranche
(tens of GW, floors −150…−500, vintage-decayed); BE ladder with the certificate stock (~7 GW solar +
~1 GW offshore) at deep floors. FR ladder unchanged — its miss is depth *frequency* (needs the
neighbour mid-band above), not ladder shape.

## NL/DE under-reach decomposition (2025, post-measured-ladders)

`scratchpad/nl_de_surplus_decompose.py` — same playbook as ES, on each zone's observed-negative hours.
Two different diseases:

**DE — boundary reached, trigger starved.** Model-DE reaches the boundary on 573/573 obs-negative
hours (mean −0.4) with `market_premium` marginal on 355 — but pins at −0.0/0.0: the §51 trigger needs
6 consecutive *strictly negative* hours to unlock the −20 rung, and nothing prints strictly negative
because the DE merchant floor was still 0.0 (the one cell the measured-ladder pass missed; measured
p50 −2.0). Chicken-and-egg: no negative prints → no runs → no trigger → no depth. German surplus is
real (demand 50.1 / must-take 47.4 GW, p10 residual −3.4 GW). Fix applied: DE merchant → −2 (measured);
open design item: vintage-correct trigger mix (the 2025 stock is 6h/4h/3h/1h blended — EEG 2017/2021 +
Solarspitzengesetz — while the workbook carries a single 6h).

**NL — the surplus does not exist in the inputs.** Model-NL prices ~88 (NL_gas_0 marginal 470/581 h),
imports saturated inward (DE→NL 92 %, BE→NL 96 % binding), balance: demand 12.2 GW vs must-take
**2.3 GW** — the ~26 GW rooftop/salderen fleet is invisible on BOTH sides of the ENTSO-E data (not in
generation: behind-the-meter; not netted from load either, or negative-noon net load would collapse
and it doesn't). No ladder can print what the balance cannot see. **Chantier: NL behind-the-meter PV
reconstruction** (installed capacity × irradiance shape netted into the NL balance) — the NL analog of
the RES-censoring work, and the gating item for NL's 35-vs-880 boundary gap.

## 2025 campaign A/B ladder: floors → +DE floor → +BTM & vintage triggers (full-year gates)

| zone | floors only | +DE merchant −2 | +NL BTM & vintage §51 | obs | boundary m<+5 (final) | o<+5 |
|------|------------|-----------------|----------------------|-----|----------------------|------|
| FR | 1311 | 1418 | 1636 | 510 | 2078 | 1066 |
| DE_LU | 292 | 897 | 1115 | 573 | 2118 | 887 |
| BE | 1194 | 1350 | 1688 | 516 | 2147 | 804 |
| CH | 624 | 927 | 1120 | 303 | 1921 | 435 |
| ES | 2808 | 2838 | 2899 | 556 | 3137 | 1458 |
| NL | 6→35 | 74 | **1254** | 581 | **1992** | 873 |
| IT_NORTH | 109 | 176 | 187 | 0 | 761 | 23 |

**Mechanisms now demonstrably live**: NL prints its SDE −20 rung (min −20, from min −1/−2 pre-BTM — the
reconstructed rooftop surplus exists and reaches the ladder); BE couples to it (BE min −2 → −20 via
import of the Dutch floor); DE's vintage classes fire as designed (the exempt+6h majority rides
through, count 897 → 1115 with *longer* episodes — FR mean episode 5.9 → 6.4 h — exactly what
grandfathering less-triggerable stock should do). FR weekend share 43–49 %, episode ~6.4 vs 5.0 h.

**Honest state of the misses, sharpened to two structural items**:
1. **Regional boundary over-reach, now uniform ×2–2.7** (CH ×4.4, IT ×33): every zone spends
   ~1900–2100 h at the RES-marginal boundary vs 435–1458 observed, and the model's boundary hours are
   REGIONALLY SYNCHRONIZED (FR/DE/BE/CH within ±5 % of each other) while reality's zones decouple.
   Prime suspect: flow-derived NTC / coincidence factors too generous exactly on surplus hours —
   which would ALSO explain the missing scarcity tail (0 model hours >200 vs 20–162 observed;
   imports shave the spikes the same way exports share the surplus) and the 15–29 € low means.
   The two tails are plausibly ONE coupling problem.
2. **Depth below −20 still absent** (model min −20 vs obs −118…−462): the deep rungs stand (−300/−500
   with real volume) but model surpluses never exhaust the shallow+mid rungs — the censored-wind /
   deep-surplus question, unchanged.
NL refinement flagged: the BTM overshoot (boundary ×2.3, mean now 18.6 LOW vs exact pre-BTM) says the
0.85-derate full-production addition over-injects — the self-consumed share may be partially netted in
the load after all; a sunny-vs-cloudy noon-load regression would measure the true double-count split.

## Regime-conditional NTC: implemented, gated, verdict — necessary physics, insufficient lever

Measured basis (`scratchpad/border_decoupling_2025.py` / `border_regime_caps.py`): on the exporter's
boundary hours, observed flows run at median 0–60 % of the static p99.5 cap (at-cap 0–6 %) while
prices decouple 34–100 % — the flow-based domain shrinks with RES, which a static cap misses.
Implementation: `assemble.regime_ntc` (regime = raw residual load < year-p20; cap = p95 of observed
flow on regime hours, clamped to static; fundamentals-only ⇒ projection-valid) + per-window-hour
arrays (`regime_cap_arrays`; the LP already broadcast time-varying caps). Real caps shrink hard where
the biases live (FR>CH 2178→818, CH>FR 1318→188).

**Gate A/B (full 2025): boundary masses −1…−9 % only (CH 1921→1800, NL 1992→1830), no scarcity tail,
means <1.5 € moved.** The over-coupling is real in the data but is NOT what synchronizes the model's
boundary hours: each zone's OWN fundamentals sit at the boundary ~2000 h — restricting exchange
redistributes little when every zone is simultaneously long from home-grown surplus. Kept (measured,
physically right, mildly positive everywhere, projection-valid) but demoted from "the lever" to
"necessary realism". The ×2 boundary bias is therefore ZONE-LEVEL: too-frequent per-zone surplus at
the bottom AND missing tightness at the top (0 model hours >200 vs 20–162 observed; means 15–29 low)
— the two live suspects, in testable order: (1) the flat annual commodity anchor (2025 gas was
front-loaded ~47 €/MWh in Q1 vs the 36 annual — winter SRMCs ~30 % too cheap ⇒ no winter scarcity,
low means) — the queued monthly-commodity-shape item, now the top candidate; (2) the price-insensitive
supply floor stack (κ·α rigidity + p10 must-run floors + must-take) overstating the bottom in every
zone simultaneously (the p10-of-observed-generation floors include economically-motivated output, an
upward-biased "must"-run).

## Measured monthly commodities: the null A/B that eliminated commodity levels entirely

Monthly TTF/EUA/coal 2019-01–2026-07 measured twice (in-browser TradingEconomics scrape + the user's
daily-based CSV export — identical ±0.01 over 2019–2025-07; CSV canonical) and wired as normalized
within-year shapes × the calibrated annual anchors (`commodities/monthly_hist.py`). **Gate A/B: null**
(means +0.2–0.3, counts identical, scarcity unchanged) — and the diagnosis of the null is the finding:
`PriceResolver.explain` shows the backtest was ALREADY serving observed monthly gas/coal/oil (World
Bank series, 2014-01–2025-12) — the "flat annual anchor" premise behind the winter-SRMC hypothesis was
wrong; only the CO2 shape was genuinely new (EUA has no open observed source — that part stays live,
plus gas/coal from 2026-01 where the WB series ends). **Consequence — stronger than the hypothesis it
killed: with correct monthly fuel prices all along, the missing scarcity tail (0 model hours >200 vs
20–162 observed) and the 15–29 € low means CANNOT be commodity levels. The single surviving suspect
for both is the SUPPLY side of the stack: availability too high / the must-run floor stack / import
tranches at the top / understated demand — winter gas at 48 €/MWh_th implies CCGT SRMC ≈ 115+, yet
model DE winter means sit at 67 with zero spikes: cheap capacity is clearing tight hours that reality
priced at 200+.**

## Scarcity-side decomposition (2025 obs>200 hours) — the top tail splits into two mechanisms

`scratchpad/scarcity_decompose_2025.py`, full-year diagnose, per zone on the observed >200 hours
(DE 162 / NL 127 / BE 74 / CH 52 / FR 31 / ES 20; 56–96 % winter, ~half evening-peak):

**The model sits at its top thermal rungs with headroom** — marginal = the zone's least-efficient gas
blocks (gas_1/gas_2, coal_2, oil) at SRMC ≈ 120–165, `set_by_constraint` ≈ 0 % (a partially-loaded
block carries the price — spare thermal exists at ~150 while reality clears 230–270 mean, 580 max).
Only CH behaves (5 h >200, max 201): its scarcity is carried by hydro WATER VALUES + imports binding
90–96 % — the one zone whose top tail has a working mechanism.

**Mechanism 1 — thermal participation overstated ~2× on tight hours (structural, measurable).** The
observed generation mix ON those hours reveals the real fleet: DE ran 15.3 GW gas at 250+ €/MWh out of
a ~30 GW fleet the model offers at 0.95 availability (~12 GW model excess; real German tight-hour
supply was 52 GW domestic + 13 GW imports for 65 GW demand); NL ran 9.1 of ~15 GW; BE 4.5 of ~7 GW.
At 250 €/MWh every AVAILABLE CCGT runs — the shortfall IS revealed non-participation (mothballing,
Netzreserve/strategic reserves outside the market, winter outages). Fix path: revealed-participation
derate per (zone, tech) measured on tight hours — same method as the whole campaign.

**Mechanism 2 — the 200+ prints themselves are the step-vii markup's mandate.** Even with correct
participation, SMC tops out near the marginal thermal cost (~150–165); reality's 250–580 on such hours
is scarcity rent above SRMC — exactly the tightness terms of the SMC→spot markup layer, which is
fitted on 2019 ONLY and not applied in backtest gates. The multi-year markup refit (2019+2022–2025)
was blocked on missing panel inputs — **the 2025/26 backfill has now unblocked it.**

Also mirrored here: FR is nuclear-marginal with headroom on half its tight hours (set_by_constraint
100 % — export borders carry the price), and the import side of tight hours does NOT show the
over-coupling seen on surplus hours (DE imports bind 64 %, reality imported 13 GW — direction right).

## Revealed-participation thermal ceilings: the scarcity tail exists, both tails move together

`blocks.participation_caps` (p99.9 of observed generation per thermal tech — validated as a true
ceiling by saturation at >150/>200 €/MWh: DE gas 0.51 of nameplate, ES 0.50, NL 0.60, BE 0.66, stable
2024/2025) clamps the neighbour stacks flex-gated. Full-2025 gate A/B (prev → now, obs):

| zone | h>200 | mean | strict neg | boundary m<+5 |
|------|-------|------|-----------|----------------|
| FR | 0 → **28** (31) | 44.9 → 49.7 (61.0) | 1627 → 1413 (510) | 2043 → 1858 (1066) |
| DE_LU | 0 → **200** (162) | 67.4 → **83.5** (89.4) | 1040 → 899 (573) | 2089 → 1893 (887) |
| BE | 0 → **74** (74 — exact) | 57.4 → 67.4 (82.6) | 1705 → 1510 (516) | 2124 → 1934 (804) |
| CH | 24 → 248 (52) | 73.8 → 84.0 (101.8) | 950 → 845 (303) | 1801 → 1651 (435) |
| ES | 0 → 67 (20) | 50.3 → **62.8** (65.2) | 2911 → 2317 (556) | 3141 → 2538 (1458) |
| NL | 0 → 187 (127) | 70.7 → **88.3** (86.9) | 1153 → 998 (581) | 1830 → 1526 (873) |
| IT_NORTH | 52 → 192 (43) | 99.4 → 103.4 (115.9) | 122 → 118 (0) | 693 → 679 (23) |

**Both tails moved toward observed simultaneously** — the signature of a true structural fix, not a
trade-off: scarcity counts now exist and three zones pass (±30 %: BE 74/74 exact, FR 28/31, DE
200/162), means closed most of the 15–29 € hole (NL +1.4 essentially exact, ES −2.4, DE −5.9), and
the boundary masses/strict counts all dropped 10–20 % as the phantom fleet stopped inflating supply.
Remaining, sharply visible: (i) import-fed zones over-print scarcity (CH 248/52, IT 192/43 — their
neighbours' clamped thermal starves them harder than reality did; likely interacts with tight-hour
import caps), (ii) residual mean gaps BE −15 / CH −18 / IT −12 / FR −11 are the step-vii markup's
mandate (SMC vs spot) — the multi-year refit (2019+2022–2025 panel, now unblocked) is the designed
consumer, (iii) the surplus side still over-reaches ~×1.7-2 and depth below −20 stays absent (wind
censoring / deep-surplus, unchanged).

## Storage re-gate on the realistic-surplus model: PASSED — default-on under flex

The machinery that failed its 2024 gate (frictionless absorption annihilated thin surpluses: DE 70→0)
re-gated on the current model (`scratchpad/f7_gate_2025_storage.py`; measured PSP envelopes + BESS
seeds). Storage-off → storage-on (obs): FR strict 1413→**530 (510)** and boundary 1858→**1042
(1066)** — essentially exact on the holdout year; CH >200 248→**39 (52)** and IT 192→**34 (43)** (the
excluded 6.7/7 GW PSP was precisely their scarcity over-print — real CH ran 3 GW PSP and EXPORTED on
its tight hours); ES boundary ×1.13, NL count ×1.29; NO annihilation (FR keeps 530 negatives vs the
3 of the 2024-era probes — coexistence now proven in backtest). `enable_storage=None` → on under
flex; explicit False retains the A/B. Named residual: the discharge side is still frictionless —
observed PSP discharge utilization is 32–54 % in the top price quartile (measured, psp_envelopes)
but not yet encoded, so model peaks shave slightly hard (DE 44 vs 162 h >200, BE 21/74, NL 44/127);
a measured discharge derate is the next dial, then the multi-year markup refit carries the residual
mean gaps (BE −18, CH −17, FR −13, IT −14).
