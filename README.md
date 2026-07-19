# PriceModeling — a bottom-up simulator of European electricity spot prices

A weather-driven, first-principles model of **hourly electricity spot prices** for France and five
European neighbours — usable both as a **backtest** against historical years and as a **20-year
projection (2026–2046)** under capacity and climate scenarios. Prices are *formed* by clearing a
7-zone economic dispatch, not fitted directly, so the model can be pushed into high-renewables,
electrified, low-thermal futures that no historical regression could reach.

```
 weathergen ─▶ demand ┐                                        backtest ◀─ ENTSO-E actuals
 (weather    (load)   ├─▶ availability ─▶ dispatch ─▶ markup ─▶
  cube)      res ─────┘   (fleet up/down)  (7-zone LP)  (SMC→spot)   projection ─▶ 2026–2046 prices
             (wind/PV)     the same weather draw feeds load AND renewables (coherent scarcity)
```

The pipeline is a chain of independent, individually validated Python packages (steps ii–vii), built on a
shared core. **Read [docs/MODELLING.md](docs/MODELLING.md) for how the whole thing works.**

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt
pip install -e powersim_core -e pricemodeling -e weathergen \
            -e demand_model -e res_model -e availability_model -e dispatch_model
pytest -q                      # offline smoke suites (synthetic data)
```

Then acquire the datasets and run the pipeline — full walkthrough in **[docs/INSTALL.md](docs/INSTALL.md)**
(SYNOP, RTE, ENTSO-E, ERA5/CDS, plant registries, TYNDP; then ETL → weather → demand/res/availability →
dispatch → prices).

## Packages

| Package | Step | Role | CLI |
|---|---|---|---|
| [`powersim_core`](powersim_core/) | — | shared substrate: glossary, RNG, Parquet lake, catalog, serialization, scenario workbook, weather cube | — |
| `pricemodeling` | ETL | SYNOP / RTE / ENTSO-E / ERA5 / registries → SQLite `master_hourly` | `python -m pricemodeling` |
| [`weathergen`](weathergen/) | ii | stochastic multi-site weather cube (EVT tails, CMIP6 trend) | `weathergen` |
| [`demand_model`](demand_model/) | iii | hybrid statistical–structural hourly French load | `demand-model` |
| [`res_model`](res_model/) | iv | weather → PV / wind / run-of-river production | `res-model` |
| [`availability_model`](availability_model/) | v | stochastic dispatchable-fleet availability (REMIT-calibrated) | `avail-model` |
| [`dispatch_model`](dispatch_model/) | vi + vii | 7-zone LP dispatch → zonal prices, and the SMC→spot markup | `dispatch-model` |

## Data artifacts

The repo carries **code, docs, and fixtures only**. A run produces (all git-ignored, rebuildable):
`data/pricemodeling.db` (the hourly `master_hourly` base), `weathergen/output/simulation.nc` (the weather
cube), the Parquet **lake** + DuckDB **catalog** under `data/`, and per-model `output/`/`models/`. The one
committed input you edit by hand is **`scenarios.xlsx`** (assumptions: growth, TYNDP capacities, RES
schemes, market rules).

## Documentation

| Doc | What |
|---|---|
| **[docs/MODELLING.md](docs/MODELLING.md)** | the end-to-end modelling approach (start here) |
| **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** | package + module map, key functions, data flow |
| **[docs/INSTALL.md](docs/INSTALL.md)** | install, datasets & credentials, running the pipeline |
| [docs/ADR.md](docs/ADR.md) | architecture decision records |
| [docs/RES_BIDDING_DESIGN.md](docs/RES_BIDDING_DESIGN.md) | the negative-price / RES bid-stack design |
| [CONVENTIONS.md](CONVENTIONS.md) | naming, units, verb semantics |
| [CONTRIBUTING.md](CONTRIBUTING.md) | dev workflow + the docs-updated-at-every-change policy |
| [REVIEW.md](REVIEW.md) | the code-review logs (Phase 0/1 hardening + Phase 2) |
| per-package `METHODOLOGY.md` / `DECISIONS.md` | step-level method + design rationale |

Generate the exhaustive per-function API reference (from the docstrings) with
`python scripts/build_docs.py` → `docs/api/index.html`.

## Development

`ruff check .` · `pytest -q` · `python tools/golden.py check` (numerical regression gate) before every
commit. See [CONTRIBUTING.md](CONTRIBUTING.md).
