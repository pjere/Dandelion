# Methodology note — `res_model` (step iv)

Calibrated weather-to-power conversion for mainland-France renewables: it turns the step (ii) synthetic
weather draws into hourly **potential** production (before market curtailment) for solar PV, onshore
wind, offshore wind and run-of-river hydro, under exogenous capacity scenarios. It consumes the **same
weather draws as the demand model** (step iii), so demand↔RES correlations propagate untouched into the
dispatch and price steps. Hybrid design: physical conversion chains whose parameters are **statistically
recalibrated** to reproduce observed national capacity factors and their distributions.

## 1. Data (all from the pricemodeling DB)

- **Production / capacity** — RTE national per-type (SOLAR, WIND_ONSHORE, WIND_OFFSHORE, HYDRO_ROR) +
  time-varying installed capacity → capacity factor; offshore also plant-level. QC flags (never fits on)
  CF-range violations, **commissioning ramp-up**, and flat-line outages.
- **Weather** — SYNOP (cloud, temperature, precip, 10 m wind) from `master_hourly`, and **ERA5 100 m
  wind + SSRD** ingested into the DB (`era5_point_hourly`). Every weather variable the predictive model
  uses is DB-sourced; the ERA5 download zips are only a raw cache.
- **Scenarios** — one multi-sheet assumptions workbook (capacity trajectories, offshore farms, cohort
  vintages, degradation/availability, spatial split, losses).

## 2. Transfer functions & fit quality

- **Wind (D1 — the 45 stations are a weak basis).** Rather than a physics power-law extrapolation, a
  **station-10 m → ERA5-100 m log-linear transfer with lag terms** is estimated on the historical
  overlap (national onshore R² 0.70, shear b 1.20). The §2.A cross-check flagged the station basis as a
  materially weaker *hourly* predictor than ERA5 (station→CF R² 0.61 vs 0.80), so — per the spec — step
  (ii) was extended to **co-generate ERA5-100 m wind** conditioned on the station draws (a spatially-
  correlated AR(1) residual over the transfer; the cube now carries `wind_speed_100m_ms`). Projection
  therefore reads a physically-realistic 100 m field, coherent with the rest of the weather draw.
- **PV irradiance (D3).** Derived from cloud with the **same** clear-sky (pvlib Haurwitz) × Kasten–
  Czeplak chain the demand model uses, so the demand↔PV irradiance signal is one and the same.
- **Offshore.** Coastal-station → farm-site 100 m via ERA5-estimated ratios; farm power curves; short
  FR history (~2 yr) → the CF anchor (~40 %) is corroborated against published operator/RTE ranges and
  its **uncertainty is documented** (§5). The modelled fleet is **time-varying** (a farm contributes only
  once commissioned) — fixing a large early bias.

## 3. Conversion chains (recalibrated)

- **PV** — cohort chain: GHI → Erbs decomposition → Hay–Davies POA (tilt/tracker mix) → NOCT cell temp →
  DC→AC clipping → losses/degradation; then a **month×hour multiplicative bias** to the observed CF
  (national ~14 %, Jul/Dec ratio ~3.7).
- **Onshore / offshore** — smoothed **aggregate power curve** (single-turbine ∝v³/rated/cut-out convolved
  with a Gaussian whose width is *fitted*, representing spatial diversity), parameterised by specific
  power; grid-searched to match the observed CF **distribution** (national ~21 %/~40 %).
- **Hydro ROR** — a small **lumped hydrological model**: observed monthly climatology + ridge blend of
  weather-derived anomalies (multi-window precip, fast/slow soil-moisture stores, PET). Leave-one-year-
  out monthly bias ~11 %. Reservoir stock and snowpack were tested and found *unhelpful* (managed Alpine
  storage ≠ rain-fed ROR); `etat_sol`'s antecedent-wetness signal is captured by the soil-moisture
  stores — so ~10–11 % is the weather-only floor (river-flow gauges, not weather-derivable, would be
  needed to go lower).

## 4. Stochastic residual layer

The deterministic chain under-disperses hourly. A residual is added per technology: **heteroscedastic by
CF level** (σ largest mid-power-curve — 2.3× the edges for wind, 2.7× for PV; ~flat for hydro), **AR(1–2)**
temporal correlation, **cross-technology** correlation (Cholesky), **bounded/beta-like** (σ→0 at the CF
edges + clipping keeps the noisy CF in range). Seeded; simulated/empirical residual std 0.9–1.0.

## 5. Projection

Per scenario × weather realization: deterministic CF (calibrated chains on the cube) → + seeded residual
→ × **vintage fleet factor** (newer cohorts have higher, flatter CF) × capacity(year) → potential
production. Realization *k* uses weather draw *k* and seed *k*, so a full trajectory is coherent with the
demand model's draw *k* — the demand↔RES co-movement (cold anticyclone = high load + low wind + clear
skies) is preserved. **PV segments** (utility/distributed/**BTM**) are kept separate and an automated
**double-counting check** reconciles Σ segments against the PV fleet and exposes BTM generation for the
demand-side netting (step iii). Output = **potential production**; economic curtailment is **step (vi)**.

**CF anchoring.** The cube's cloud/precip (ERA5-derived) differ from the SYNOP fields the PV/hydro chains
were calibrated on, shifting the *level*. Each technology's projected CF is anchored to its calibrated
national mean (a single per-tech scale; wind's came out ≈1, confirming it was already consistent). Shape
and demand-coherence are preserved. Outputs: partitioned Parquet with git/config/workbook/cube hashes +
weather-draw ID + seed.

## 6. Validation (§6)

`res-model validate` checks CF anchors/monthly-bias, CF **duration curves**, wind **ramp** tails, projected
**inter-annual** wind dispersion, **vintage** sanity, offshore ranges, and the **cross-variable killer
test**: load–wind / load–PV seasonal correlations and **Dunkelflaute** event counts (rolling 72 h wind
CF<15 % & PV CF<5 % in top-decile demand), history vs projection — the correlation structure that makes
long-run price distributions realistic.

## 7. Curtailment perimeter & known limitations

- **No curtailment here** — output is potential production; negative-price/grid curtailment is an economic
  decision in **step (vi)**. Only intrinsic technical losses/availability are applied.
- **No day-ahead forecast error** — spot prices form on D-1 forecasts, but using actuals is an accepted
  first-order simplification, to revisit if step (vii) shows systematic imbalance-related spreads.
- **Offshore** history is short (~2 yr) → CF carries the widest uncertainty (documented, anchored to
  published ranges).
- **ROR** is at the ~10–11 % weather-predictability floor; sub-decile monthly accuracy needs river-flow
  state not available for a 20-yr projection.
- **National** calibration/aggregation for now; the code keeps region/cohort resolution for later
  intra-France network extensions.
- The shipped workbook holds **illustrative** capacity/vintage trajectories — replace before using the
  projected levels.
