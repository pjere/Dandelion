# Methodology note — `demand_model` (step iii)

A long-term **hourly power-demand model for mainland France**, feeding a downstream spot-price
model. It is deliberately *not* a black-box extrapolation: a transparent statistical core is
calibrated on history, then carried forward by an explicit **structural projection layer** driven by
scenario assumptions and synthetic weather, with a **stochastic residual layer** restoring realistic
hourly variability. This note summarises the method; `DECISIONS.md` is the exhaustive decision log.

## 1. Scope & perimeter

- **Target** = RTE *REALISED* consumption **− pumping** (`pumping = −min(HYDRO_PUMPED_STORAGE, 0)`).
  Pumping is a dispatch decision handled in the price/dispatch step, so it is removed here.
- **Resolution** hourly (15-min REALISED aggregated); **grid losses embedded**; **gross demand** with
  **explicit behind-the-meter PV netting**. Timestamps stored UTC; calendar features in Europe/Paris
  (DST 23/25-h days handled explicitly).
- **History** 2015-01 → 2026-06 for calibration; **projection** 2027 → 2046.

## 2. Data

- **Load** & **pumping** from the `pricemodeling` SQLite DB (RTE series).
- **Weather** — 42 metropolitan stations (temperature, wind, cloud, humidity), same tidy contract for
  historical observations and the synthetic `weathergen` cube, so calibration and projection are
  identical by construction.
- **Scenario drivers** — one multi-sheet, multi-scenario **assumptions workbook** (tidy long format),
  one sheet per driver family; every sheet validated on load (pandera contracts).

## 3. Statistical core (calibration)

`load(t) = f_base(t) + f_heat(t) + f_cool(t) + f_light(t) + ε(t)`, estimated as a **ridge-on-splines
additive model** (numpy closed-form, unpenalised intercept). Components are kept **structurally
separable** so the projection layer can rescale each independently.

- **Effective temperature** `T_nat` — station-weighted national temperature, smoothed with two
  exponential kernels (~12 h and ~60 h, building thermal inertia) plus lagged daily means (D-1, D-2).
- **f_base** — day-type × hour load shape (Mon / Tue-Thu / Fri / Sat / Sun / holiday / bridge),
  month seasonality, season × hour shape modulation, school-vacation fraction, special days
  (day before/after holidays, Christmas week, August), a linear trend and a permanent **post-2022
  level step** (structural demand drop: energy crisis + sobriety + deindustrialisation).
- **f_heat** — heating-degree terms on the slow temperature × hour (weekday/weekend), a fast-response
  term (12 h smoothing, morning ramp), lagged-daily-temperature terms (thermal mass) and a cold-tail
  term. Thermosensitivity thresholds are **estimated from the data** (heat 14.5 °C, cool 20.5 °C).
- **f_cool** — cooling-degree terms × hour (afternoon AC).
- **f_light** — cloud-driven GHI deficit × hour. Irradiance is derived as **clear-sky GHI (pvlib
  Haurwitz) × Kasten–Czeplak cloud attenuation**, available in both history and the weather generator.

**No autoregressive load** is used — lagged actual load cannot exist 20 years out, and it would make
the model projection-invalid.

**Acceptance (hold-out 2025):** winter gradient **−2.30 GW/°C** (RTE ~2.4 band ✓), in-sample MAPE
**2.97 %**, hold-out **3.52 %**, bias −0.9 %. Sub-3 % *hold-out* is unreachable without autoregression;
~3.3-3.5 % is the irreducible weather+calendar floor for a projection-valid model.

## 4. Stochastic residual layer

`ε(t)` is persistent and heteroscedastic. It is modelled as
`z(t)=ε/σ(bucket)`, `z(t)=φ₁z(t-1)+φ₂z(t-2)+η(t)`, `ε_sim=σ(bucket)·z_sim`:

- **σ** per (season × local-hour × weekend) bucket — 192 cells, 1.03-3.12 GW (winter evenings ≈ 3×
  summer nights);
- **AR(2)** persistence (φ=[1.215, −0.323], lag-1 autocorr ≈ 0.92), companion-matrix stabilised;
- **bootstrap innovations** (fat-tailed), fully seeded/reproducible.

Seeded draws reproduce empirical lag-1 autocorrelation (0.915 vs 0.919) and variance (ratio 1.01).

## 5. Structural projection layer

For each scenario × weather realization:

`load_net(t) = Σ_g D_g(year)·component_g(t) + EV + electrolysis + datacentres + other − BTM-PV + ε(t)`

- **Base** — the calibrated trend is **frozen at the anchor year (2026)**; the base is then scaled by a
  composite structural index (population + tertiary/GDP + industry) net of autonomous efficiency.
- **Heating** — the calibrated heating gradient is **reshaped** by an electric-heating index
  `S = (resistance_stock + HP_stock/COP) · renovation_index` (resistance→HP substitution, new HP
  electrification, renovation), *not* a bottom-up HP add → no double-count, weather correlation kept.
- **Cooling / lighting** — AC-penetration ratio / population ratio.
- **Bottom-up new loads** — EV (fleet × km × kWh/km per segment, shaped by smart/home charging
  archetypes), electrolysis (capacity × load factor), datacentres, other point loads.
- **BTM-PV** — self-consumption of **only post-anchor incremental** PV is netted (RTE REALISED already
  excludes today's behind-the-meter PV), using the same irradiance draw.

The demand model is a **per-weather-scenario transducer**: one weather realization → one coherent
demand trajectory. The Monte-Carlo risk distribution lives in the *outer* weather→demand→price loop
(each full trajectory is one coherent draw), so the model does not collapse a weather ensemble
internally. A single draw is `deterministic_net + one residual draw`, seeded off the draw index for
reproducibility:

```
Projector(config).trajectory(scenario="reference", realization=k, seed=k)   # weather k ⊕ ε k
```

Outputs: per-scenario annual **energy**, **deterministic peak**, a **within-weather residual-peak
diagnostic** (`peak_gw_residdiag_p50/p95` — varies only ε at fixed weather; a QC band on the residual
layer, *not* the weather-driven risk envelope), load factor, per-component energy, and the
deterministic hourly load. The price step draws coherent trajectories via `Projector.trajectory`.

## 6. Validation

`demand-model validate` runs an acceptance suite (HTML report) covering: gradient/MAPE/bias,
additive separability and component signs, **cold-spell stress** (coldest 1 % of days + sustained
cold-spell inertia), residual heteroscedasticity/persistence/spell length, and projected
energy/peak/load-factor plausibility. Current status: **18/18 hard checks pass**, one soft WARN (the
hold-out MAPE aspiration, above).

## 7. Usage

```
demand-model init-workbook     # write the assumptions workbook template
demand-model calibrate         # fit the statistical core (mean + residual) on history
demand-model project           # project every scenario from weathergen weather + drivers
demand-model validate          # acceptance checks + HTML report
```

## 8. Known limitations

- No explicit price-elasticity of demand (handled as flexible bids in the dispatch step).
- Extreme-cold thermosensitivity relies on the few cold spells in 2015-2026 plus the weather
  generator's synthetic cold spells.
- BTM-PV and new-large-load trajectories are exogenous (workbook), not endogenous to price.
- The shipped workbook holds **illustrative placeholder trajectories** — replace with real scenario
  assumptions before using the projected levels.
- **By design**, the model maps one weather scenario → one demand trajectory; the weather-driven risk
  distribution is produced by the outer Monte-Carlo over weather draws (in the price step), not
  internally. The `residdiag` peak band is a QC diagnostic of the residual layer, not that risk
  envelope. `Projector.features(realization=k)` already selects ensemble members if the weathergen
  cube carries them.
