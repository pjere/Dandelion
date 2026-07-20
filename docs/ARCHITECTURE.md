# Architecture & module map

> The **prose** map of the codebase: packages, their public modules, and the key entry-point functions
> (one line each). The **exhaustive per-function reference** is generated from the docstrings — run
> `python scripts/build_docs.py` (see §5) — so this file stays a readable index, and the docstrings are
> the single source of truth for signatures and detail. Keep this file in sync when a module's role or a
> package's public surface changes ([CONTRIBUTING.md](../CONTRIBUTING.md)).

## 1. Repository layout

```
PriceModeling/
├── powersim_core/        shared library (glossary, RNG, lake, catalog, serialize, scenario, cube)
├── pricemodeling/        ETL: SYNOP / RTE / ENTSO-E / ERA5 / registries → SQLite master_hourly + lake
├── weathergen/           step ii — stochastic weather cube (simulation.nc)
├── demand_model/         step iii — hourly French load
├── res_model/            step iv — weather → PV/wind/ROR production
├── availability_model/   step v — stochastic dispatchable-fleet availability
├── dispatch_model/       steps vi + vii — 7-zone LP dispatch → prices, and the SMC→spot markup
├── config/               pricemodeling settings.yaml + rte_catalog.yaml
├── docs/                 MODELLING.md, ARCHITECTURE.md, INSTALL.md, ADR.md, RES_BIDDING_DESIGN.md, api/
├── tools/golden.py       golden-output harness (numerical regression gate)
├── golden/baseline.json  frozen model-output digests (committed fixture)
├── scripts/              maintenance scripts (backfill_entsoe.py, build_docs.py)
├── scenarios.xlsx        the single hand-edited assumptions workbook (ADR-5)
├── tests/                pricemodeling / ETL tests
├── CONVENTIONS.md  REVIEW.md  CONTRIBUTING.md  README.md
└── requirements.txt  pyproject.toml  .pre-commit-config.yaml
```

Each `*_model/` and `powersim_core/` and `pricemodeling/` is an installable package with its own
`pyproject.toml`, tests, and (for the models) `README.md` + `METHODOLOGY.md` + `DECISIONS.md`.

## 2. Dependency graph (ADR-8)

Packages couple only through **shared data artifacts** and through the **owning package's public API**
(lazily imported at the call site), never by reaching into another package's internals or writing into its
store. The direction is acyclic:

```
powersim_core  ◀── everything
pricemodeling  ◀── all models (DB + REMIT + registries read paths)
weathergen     ◀── demand_model, res_model            (the simulation.nc cube)
demand_model ┐
res_model    ┴─▶ dispatch_model.weather_shapes         (FR coherent projection draw)
availability_model ─▶ dispatch_model                   (fleet availability feed)
```

All seven packages are installed **editable** so these imports resolve without path hacks.

## 3. Data flow & shared artifacts

| Artifact | Producer | Consumers | Where |
|---|---|---|---|
| `master_hourly` (+ `entsoe_*`, `rte_*`, registries) | `pricemodeling` | all models | SQLite `data/pricemodeling.db` |
| `simulation.nc` weather cube `(time, station, variable)` | `weathergen` | demand, res | `weathergen/output/` |
| Parquet **lake** + DuckDB **catalog** | all models | dispatch, markup | `data/lake/`, `data/powersim.duckdb` |
| `scenarios.xlsx` (28 prefixed tabs) | hand-edited | all models | repo root |
| canonical `plant_registry` (reference layer) | `pricemodeling.registries` | dispatch stacks, RES schemes | lake `reference/` |
| `markup_model.json` (fitted wedge) | `dispatch_model.markup` | projection | `dispatch_model/reports/` |
| `golden/baseline.json` | `tools/golden.py` | CI gate | repo root |

## 4. Package module maps (key public entry points)

Docstrings carry the full contract; below is the "where do I look" index.

### `powersim_core` — shared substrate
- `glossary` — canonical names / units / verb semantics (the `CONVENTIONS.md` rules in code).
- `rng` — `substream(seed, draw, label)`, `draw_rng(seed, draw)`: the SeedSequence RNG authority.
- `lake` — `write_table` / `read_table`: the sole output-I/O authority (partitioned Parquet).
- `catalog` — builds `data/powersim.duckdb` (one view per dataset + a run ledger).
- `schemas` — the centralized pandera `validate` + shared column builders.
- `serialize` — `save_params`/`load_params`, `save_dataclass`/`load_dataclass` (JSON + npz, never pickle).
- `scenario` — `load_model_sheets` / `load_sheet` + `snapshot()`: the `scenarios.xlsx` accessor.
- `registry` — canonical `plant_registry`: `read`, `active(year)`, `apply_overrides`, `write`.
- `weather_cube` — the single cube loader (national-mean reduction, ensemble dims).
- `time_grid`, `units`, `meta` — leap-day policy, unit helpers, run-metadata hashing.

### `pricemodeling` — ETL (`python -m pricemodeling <cmd>`)
- `pipeline` — the Typer CLI: `init-db`, `rte-token`, `extract-meteo`, `extract-rte`, `extract-entsoe`,
  `ingest-remit`, `reconcile-units`, `build-master`, `all`, `status`.
- `meteo/synop.py`, `meteo/stations.py` — SYNOP download/parse → `synop_obs`, `dim_station`.
- `rte/` — `auth.py` (OAuth2), `client.py` (chunk/retry/cache), `catalog.py`, `extract.py`.
- `entsoe/` — `series.py` (prices/load/gen/flows), `unavailability.py` (**REMIT**:
  `ingest_unavailability`, `reconstruct_daily_availability`, `zone_availability_stats`,
  `nuclear_unavailable_mw`), `prices.py`.
- `registries/` — `mastr.py` (DE), `odre.py` (FR), `opsd.py`, `repd.py`, `cohort.py`, `download.py`
  (resumable bulk downloads) → canonical registry rows.
- `reconcile/units.py` — EIC-keyed production-unit reconciliation.
- `merge/build_master.py` — the hourly `master_hourly` grid (UTC, DST-aware).

### `weathergen` — step ii (`weathergen <cmd>`)
- `cli.py` — `fit`, `simulate`, `fetch-cmip6-deltas`.
- `io.py` — SYNOP/ERA5 ingestion + QC. `era5_arco.py`, `era5_cds.py` — ERA5 point time-series.
- `climatology.py` → `transforms.py` → `marginals.py` (EVT) → `dependence.py` (copula + EOF-VAR) →
  `trend.py` (QDM) → `simulate.py` → `validate.py`: the fit→simulate pipeline.
- `wind100.py` — 100 m wind co-generation (`fit_wind100`). `cmip6_cds.py` — climate deltas.

### `demand_model` — step iii (`demand-model <cmd>`)
- `cli.py` — `init-workbook`, `calibrate`, `project`, `validate`.
- `features/` — effective temperature, calendar (`calendar.py`), irradiance.
- `calibration/` — component decomposition. `residual/model.py` — stochastic residual.
- `projection/engine.py` (`project_trajectory`), `projection/heatpump.py` (COP-vs-temperature),
  EV/electrolysis/BTM-PV bottom-up modules.

### `res_model` — step iv (`res-model <cmd>`)
- `cli.py` — `init-workbook`, `calibrate`, `project`, `validate`.
- `io/era5.py` — ERA5 100 m wind + irradiance. `transfer/` — station→ERA5-100 m + GHI.
- `conversion/` — PV / onshore / offshore / ROR chains. `calibration/` — to national CFs.
- `stochastic/model.py` — residual layer. `projection/engine.py` (`Projector.production`),
  `projection/drivers.py`.

### `availability_model` — step v (`avail-model <cmd>`)
- `cli.py` — `init-workbook`, `calibrate`, `project`, `validate`.
- `io/` — fleet registry, outage inference from per-unit production, REMIT.
- `calibration/` — `planned.py`, `forced.py` (`calibrate_forced`), `common_mode.py`, `derating.py`,
  `inflows.py`; `model.py` (`CalibratedAvailability`).
- `projection/` — `planned_scheduler.py` (concurrency-capped nuclear cadence), forced/common-mode
  processes, `engine.py` (coherent draws → Parquet).

### `dispatch_model` — steps vi + vii (`dispatch-model <cmd>`)
- `cli.py` / `pipeline.py` — `build-inputs`, `run` (projection), `backtest`, `validate`.
- `commodities/model.py` — gas/CO₂/coal/oil scenario trajectory, per-zone gas basis (`zone_prices`);
  `commodities/observed.py` — **dated observed price store** + `ingest_csv` (licensed or public, unit-
  normalised); `commodities/resolve.py` — `PriceResolver`: **daily → monthly observed → scenario**
  precedence with provenance (`explain`, `coverage_report`); `commodities/public_sources.py` — free
  World Bank + ECB FX fallback; `commodities/gas_rules.py` — period gas rules (Iberian cap, hub basis).
- `stacks/fr_stack.py` (`build_fr_stack`, `srmc`), `stacks/costs.py` (SRMC constants / `EFF_RANGE`).
- `neighbours/blocks.py` — aggregated stacks (`build_neighbour_stack`), DE unit-level
  (`build_de_unit_stack`), measured CHP must-run, `neighbour_netload`, zone aggregates (DE_REST).
- `lp/multi_zone.py` (`solve_multizone` — the LP, price = balance dual; `_BACKEND` selects the solver),
  `lp/highs_solver.py` (the default **direct-highspy** fast path — builds the same LP as sparse arrays,
  byte-identical duals, no linopy per-window rebuild), `lp/single_zone.py`.
- `res_schemes.py` — RES subsidy bid stack + §51 trigger fixed point (`solve_with_triggers`).
- `rules.py` — per-zone/period market rules (IT/ES negative-price floors). `hydro/guide_curves.py`.
- `rolling/windows.py` — shared per-window assembly (`fr_window`, `nb_window`, `fr_stack_base`).
- `rolling/backtest.py` (`run_backtest`), `rolling/projection.py` (`project_year`,
  `project_trajectory`), `rolling/assemble.py` (`flow_derived_ntc`, coincident NTC).
- `rolling/montecarlo.py` — **parallel Monte-Carlo** ensemble (`run_ensemble`, `ensemble_stats`): draws
  across a process pool, each byte-identical to serial (deterministic `powersim_core.rng` per draw).
- `markup.py` — the SMC→spot wedge (`build_panel`, `fit_markup`, `apply_markup`).
- `tyndp.py` — capacity trajectories (`load_tyndp`, `tyndp_factors`, `flex_capacity_mw`).
- `scheme_evolution.py` — year-varying RES tranches (`scheme_shares`, `trigger_hours`).
- `weather_shapes.py` — #77 coherent projection draws (`fr_draw`, `all_weather_shapes`,
  `NeighbourWeatherModel`). `neighbour_availability.py` — #80 REMIT-derived per-draw derating.

## 5. Generating the full per-function API reference

```bash
pip install -r requirements-dev.txt   # installs pdoc (or: pip install pdoc)
python scripts/build_docs.py          # renders docs/api/index.html for all 7 packages
```

`docs/api/` is **git-ignored** — it is regenerated from the current docstrings on demand, so the
per-function reference is never stale and never churns the repo. The prose docs (`MODELLING.md`, this
file, per-package `METHODOLOGY.md`) are the curated layer on top.
