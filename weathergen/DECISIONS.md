# DECISIONS — weathergen

Running log of every modeling choice and its rationale. Each `# DECISION:` in the
code points back here. Resolved decisions are dated; open ones are surfaced at the
relevant phase for sign-off.

## Resolved (kickoff, 2026-06-29)

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D0.1 | Lengthen record with ERA5? | **Yes** (user will provide ERA5 hourly at station coords) | 12 yr is too short for tails + climate baseline; station = truth, ERA5 = extend tails/seasonality. Ingestion designed with an ERA5 fusion hook (`data.era5` in config); wired before Phase 4 (EVT) where it matters most. |
| D0.2 | Solar / GHI handling | **Exclude solar** | SYNOP data has NO measured irradiance (only cloud cover). No `pvlib`/clear-sky branch in v1. (ERA5 `ssrd` could enable it later if desired.) |
| D0.3 | Variable set | temperature, wind speed, sea-level pressure, dew point, humidity, cloud cover (total & low), **precipitation** | User opted into precip despite the intermittency flag → modeled as occurrence+intensity censored-latent (see D-precip), not a naive Gaussian. |
| D0.4 | Station scope | **Metropolitan France only (~42)** | One coherent spatial domain; Matérn-over-great-circle-distance is meaningful. Overseas/sub-Antarctic stations excluded (disjoint fields). |
| D-precip | Precipitation in a Gaussian-copula SWG | Censored-latent hurdle: occurrence via latent threshold (per-site wet probability), intensity via transformed amount; keeps it inside the copula. | Precip is intermittent and non-Gaussian; a point mass at zero is handled by censoring the latent Gaussian. Detailed at Phase 4/5. |

## Phase 1 (io + QC + ERA5), 2026-06-29

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D1.1 | QC physical bounds | per-variable in `config.yaml` (user-approved) | range-flagged → NaN; never imputed into fit. |
| D1.2 | Stuck-sensor (flat-line) | run of ≥ 24 identical non-NaN values flagged; legitimate zero-runs (wind calms, dry precip) exempt | catches frozen sensors without flagging real calms. |
| D1.3 | Spike detection | robust z (MAD) on first differences, |z|>8 AND up-then-down (isolated) | flags isolated outliers, not legitimate ramps. On real data: 3–11 per variable (not over-flagging). |
| D1.4 | Short-gap handling | ≤ 3 h linear-interpolated + flagged `F_INTERP`; longer gaps left NaN, excluded from fit | no silent imputation into the training set. |
| D1.5 | ERA5 bias-correction | **monthly mean+std matching** (ERA5 → station scale, per month) | light, robust, absorbs ERA5 unit/scale differences. Flagged for upgrade to quantile-mapping (ties into Phase 4/6 sdba) if tails need it. |
| D1.6 | ERA5 fusion mode | station = truth; ERA5 only **extends** before station start + **infills long gaps** (all flagged `F_ERA5_EXTEND` / `F_ERA5_INFILL`) | never overwrites observed station data. |
| D1.7 | ERA5 active from Phase 1 | code path live; runs on real ERA5 when a NetCDF is dropped at `data.era5.path` or CDS creds are provided | tested against a synthetic ERA5 fixture in the meantime. |

Real-data QC snapshot (42 metro stations, 2014-12→2026-01, 97 560 h):
missing median 9.9% (min 3.1%, max 47.7% — one sparse station flagged for possible exclusion);
QC-removed 0.76 M cells; short-gap-interpolated 0.31 M.

## Phase 2 (climatology), 2026-06-30

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D2.1 | Harmonic basis | 3 seasonal × 3 diurnal harmonics, **interacted** (49 terms), hours in **local solar time** | diurnal amplitude varies by season (interaction); LST aligns the diurnal phase across longitudes. |
| D2.2 | Heteroscedastic variance | fit log σ²(doy,lst) on the same basis; OLS-on-log(r²) bias-corrected by +1.2704 (E[log χ²₁]) | seasonal/diurnal variance is strong; constant-σ would be wrong. |
| D2.3 | Precip under harmonic standardization | apply uniformly; **accept non-unit residual** (z_std≈2.8) | intermittency can't be standardized by one σ — the mean cycle is removed here, the distribution is handled by the hurdle/censored marginal in Phases 3-4. Flagged, not hidden. |

Real-data acceptance (42 stations, 2014-12→2026-01): fitted-vs-observed mean-surface RMSE
temp 0.28°C / wind 0.12 m/s / pressure 0.49 hPa / dewpt 0.32°C / RH 0.95% / cloud 1.5%;
residual z_mean≈0, z_std 0.97–1.02 (continuous vars), |binned hour/month means|<0.11,
binned std∈[0.91,1.13]. No leftover diurnal/seasonal structure. Figure: reports/phase2_climatology.png.
ERA5 not yet folded in (download in progress) — climatology will be refit on the extended record when it lands.

## Phase 3 (transforms), 2026-06-30

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D3.1 | Transform order | transform RAW **before** climatology (transform→deseasonalize) | logit/hurdle are only meaningful on raw bounded/intermittent values; identity-transformed vars (temp/pressure/dewpt) unchanged, so Phase-2 sign-off holds for them. |
| D3.2 | humidity | **DERIVED from T + dew point** (Magnus), dropped as a modeled variable | logit backfires (RH saturates at 100% → skew −0.72→+3.72); RH is redundant given T+Td. Modeled set = 6 (temp, wind, pressure, dew point, cloud, precip); RH appears in output, derived in simulate.py. (User didn't object to the recommended option.) |

Real-data acceptance (42 stations): map / skew_raw→skew_tr / inv-error / climatology z_std —
temp Identity 0.21→0.21 / 0 / 0.98 · wind **YeoJohnson 1.43→0.00** / 5e-14 / 0.99 ·
pressure Identity −0.44 / 0 / 1.00 · dewpt Identity −0.34 / 0 / 0.98 · cloud Logit −1.67→−0.18 / 1e-4 / 1.09 ·
precip Log1p 15.8→5.5 / 7e-15 / 2.22 (intermittent → Phase-4) · humidity Logit −0.72→**+3.72** / 1e-4 / 1.29 (see D3.2).

## Phase 4 (EVT marginals), 2026-06-30

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D4.1 | GPD engine | scipy.stats.genpareto for the fit + exact invertible splice | full control over a monotone/continuous/invertible CDF; pyextremes pluggable for deeper threshold diagnostics (an MRL helper is provided). |
| D4.2 | Threshold | config quantile (0.95) per tail; MRL/stability diagnostic available | simple, stable; can switch to MRL-selected thresholds per variable if needed. |
| D4.3 | Season-stratified tails | **pooled tails on standardized anomalies** (default) | the climatology σ(doy,lst) already restores seasonal modulation; return-level diagnostics show good fit with pooled tails. Revisit if residual seasonal tail structure appears. |
| D4.4 | Precip marginal | **censored/hurdle**: point mass p_dry + empirical+GPD wet body; dry frequency exact; sub-threshold simulated precip hard-zeroed in Phase 7 | correct occurrence + intensity within the copula latent. |

Real-data acceptance (station S0): round-trip F⁻¹(F(z)) err ~1e-14, CDFs monotone; GPD shape ξ
temp −0.15 / wind −0.09 / pressure −0.05 (bounded light tails); fitted return levels overlay
empirical and extrapolate past the sample max (reports/phase4_marginals.png). precip p_dry=0.854.
**Tails fit on the 12-yr station record** — they will be re-fit on the ERA5-extended record when the
download completes (this is exactly what most reduces tail uncertainty; warning carried until then).

## Phase 5 (dependence), 2026-07-01

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D5.1 | Copula family | **Gaussian** (VAR with Gaussian innovations) | correct + marginal-consistent with the Φ PIT; auditable. t-copula (joint tail dependence) offered pending the Phase-8 tail-dependence diagnostic — needs t-consistent margins, deliberate follow-up. |
| D5.2 | Reduction rank k | EOF modes for ``eof_variance``=0.90 → **k=103** of 252 dims; discarded-mode variance added back as per-dim white residual (keeps margins ~N(0,1)) | tractable VAR while preserving per-column unit variance for the PIT. |
| D5.3 | Temporal order p | **VAR(6)** (tradeoff) | p=2 under-persisted (ACF MAE 0.112); p=6 → 0.090 and runs in time; p=12 too slow with the dense VAR. Higher persistence is a tunable (var_order) knob. |

Real-data acceptance (5→3-yr sim vs observed): corr-vs-distance binned MAE (temp) **0.015**;
cross-variable corr MAE (S0) 0.084; temperature ACF MAE to 48 h **0.090** (shape + 24 h diurnal
bump captured, slightly under-persistent at 48 h). Figure: reports/phase5_dependence.png.

## Phase 6 (external climate trend / QDM), 2026-07-01

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D6.1 | QDM engine | direct numpy QDM (time-varying, per-month, quantile-wise) | transparent; xclim.sdba is the drop-in production alternative once real CMIP6 runs are ingested. |
| D6.2 | Per-variable mode | temp/dewpt/cloud ADDITIVE, wind/precip MULTIPLICATIVE, **pressure NOT trended** | physical consistency (pressure ~stationary; non-negatives multiplicative). |
| D6.3 | Trend variability | full quantile-delta curve (default) vs median-only | preserves variance/tail changes (intensification), toggle `trend_variability`. |
| **OPEN** | SSP scenario + horizon + real CMIP6 deltas | **needs user input** | trend is OFF by default; needs the SSP (e.g. SSP2-4.5 / SSP5-8.5), target/horizon years, and the CMIP6 deltas sourced (CDS `projections-cmip6`, offered). Delta file format: npz with `quantiles` (nq) + `<var>` (12, nq). Example saved: reports/example_cmip6_deltas.npz. |

Acceptance (prescribed tail-intensifying delta): trend OFF stationary (mean +0.15°C, p99 −0.19°C);
trend ON mean +2.96°C, **p99 +6.65°C** (tail ≫ mean = intensification); transition monotone/smooth.
Figure: reports/phase6_trend.png.

## Phase 7 (simulation engine), 2026-07-06

| # | Decision | Choice | Rationale |
|---|----------|--------|-----------|
| D7.1 | SSP + horizon | **simulation-time inputs** (`--ssp`, `--target-year`, default 2050) — one fitted model serves any scenario; trend rebuilt at simulate time | matches the spec (scenario/horizon are inputs). |
| D7.2 | Degenerate columns | station×variable with too little data (sparse station) → standard-normal marginal fallback (never NaN) | avoids NaN; improves automatically once ERA5 infills the sparse station. |
| D7.3 | CMIP6 model | **MPI-ESM1-2-LR** (EC-Earth3 has broken roocs subsetting on CDS → RoocsValueError) | reliable daily area-subset retrieval. |

Real acceptance: 20.0 yr hourly × 42 stations × 7 variables, **0 NaN**, all constraints satisfied,
deterministic under seed, NetCDF provenance embedded (seed + full config + ssp + target + trend flag).
Realism: France-mean Jan 5.9°C / Jul 21.9°C, extremes −5.9…37.1°C, 90.6% dry hours.

## Phase 8 (validation suite), 2026-07-06

Full HTML report (reports/validation_report.html) with embedded figures + PASS/WARN flags:
marginals/QQ, diurnal+seasonal surface, ACF, cross-variable corr, inter-station corr-vs-distance,
return levels + threshold exceedances, and spell/persistence (heat / calm-wind / dry, weighted ×3).

Real acceptance (20-yr sim vs observed) — **PASS**: temp/wind/pressure/dewpoint marginals,
diurnal+seasonal RMSE 0.39°C, ACF MAE 0.096, cross-var MAE 0.088, corr-vs-distance MAE 0.017,
temp>30°C & wind>15 m/s exceedance rates. **WARN (surfaced, not buried):**
- precip intensity tail too heavy (sim p99.9 9.1 vs obs 5.8 mm/h) → Phase-4 precip marginal needs a
  lighter/​capped wet tail (D8.1, open).
- spell persistence under-captured (dry/calm/heat spells ~½ observed length) → VAR temporal memory
  too short; raise `var_order` or add an AR on the leading slow EOF modes; ERA5 extension will also help (D8.2, open).
- Fixed during Phase 8: pressure/precip pooled-variance blow-up traced to the sparse degenerate
  station → cross-station-mean climatology fallback (climatology.fit) gives it physical values.

The suite works as intended (surfaces real deficiencies); the two WARNs are model refinements.

## Post-Phase-8 refinements, 2026-07-07

Validation-driven fixes (score 10/21 → 17/21 and improving):

| # | Issue found by validation | Fix | Result |
|---|---------------------------|-----|--------|
| R1 | Pressure/precip variance +136%/+513% | dependence re-imposes fitted PC std at simulate (near-unit-root VAR was inflating slow modes) | pressure std +2% (PASS) |
| R2 | Spells ~half observed (persistence) | var_order 6→12 (VAR fit on a bounded recent window to stay tractable on 47 yr) | calm-wind & heat spells now PASS; dry spells improved (~65% of obs, still WARN) |
| R3 | Precip wet mean/dry-freq wrong | dry hours → exact 0 via deep-negative sentinel (drop the hard-zero threshold); precip tails body-only (EVT log→expm1 explodes); GPD ξ capped ≤0.4 | precip mean/p99/occurrence match |
| R4 | Precip std harsh (rare-extreme dominated) | precip bound 250→**60 mm/h** (physical France hourly); marginal check judges precip on mean+p99+wet-frequency, not std | — |
| R5 | ERA5 too slow (monthly gridded) | **ARCO point time-series** (`era5_arco.py`): 1 request/station = full 47-yr record; ~42 requests vs 576 | record 12→**47 yr**; tails + sparse station rescued |
| R6 | Degenerate sparse-station columns | climatology borrows cross-station-mean climatology; marginals fall back to standard-normal | physical values, no NaN |

Final validation: **18/21 weighted** (was 10/21). ALL marginals PASS (incl. precip: mean/p99/wet-freq
match), diurnal RMSE 0.47°C, ACF MAE 0.041, cross-var 0.051, corr-distance 0.039, both exceedances PASS,
calm-wind + heat spells PASS. **Only remaining WARN: dry-spell persistence** (sim mean 20.8h vs obs 32.1h,
~65% — precip-occurrence clustering is the hardest SWG statistic; would need a dedicated occurrence
memory / higher precip-latent persistence). Documented, not hidden.

## Open (to resolve at their phase, with sign-off)

- **Phase 1** — confirm physical QC bounds per variable (config defaults proposed).
- **Phase 4** — season-stratified tails if residual seasonal tail structure remains after σ(doy,hour); GPD threshold per variable.
- **Phase 5** — copula family (t vs Gaussian) and EOF rank k, driven by tail-dependence diagnostics.
- **Phase 6** — SSP scenario(s), horizon, and whether to trend variability (not just mean). These are simulation inputs.

## Conventions

- All timestamps stored UTC; diurnal model uses **local solar time** (per-station longitude offset).
- Single seeded `numpy.random.Generator` threaded through fit + simulate.
- Fit is serialized to `models/`; simulation loads and is cheap.
- Every clip / imputation / assumption is logged in the validation report, never hidden.
