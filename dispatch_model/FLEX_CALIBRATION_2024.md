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
