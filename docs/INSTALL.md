# Installation, datasets & running the pipeline

This guide takes you from a fresh clone to a working spot-price simulation. Three stages:
**(1)** install the code, **(2)** acquire the datasets from their sources, **(3)** run the pipeline.

> The repository ships **code, docs, and fixtures only** — no data. Every dataset below is downloaded
> from its public source by the pipeline itself and cached locally under `data/` (git-ignored). Expect the
> full raw data to reach tens of GB (ERA5 dominates). You can work on short windows / few draws for a
> quick end-to-end run — see §4.

## 1. Environment

Requires **Python 3.11+**. A LP solver (HiGHS) ships with `highspy` (a dispatch dependency).

```bash
git clone <repo-url> PriceModeling
cd PriceModeling

python -m venv .venv
source .venv/bin/activate           # Windows PowerShell: .\.venv\Scripts\Activate.ps1

pip install -r requirements.txt      # ETL runtime deps
pip install -r requirements-dev.txt  # tests, lint, pre-commit, pdoc
```

## 2. Install the packages (editable, in dependency order)

The seven packages import each other through their public APIs, so all must be installed editable
(ADR-8). Order matters only in that `powersim_core` and `pricemodeling` come first:

```bash
pip install -e powersim_core
pip install -e pricemodeling
pip install -e weathergen
pip install -e demand_model
pip install -e res_model
pip install -e availability_model
pip install -e dispatch_model
```

Each pulls its own runtime dependencies from its `pyproject.toml` (numpy/pandas/scipy/xarray, plus
`linopy`+`highspy` for dispatch, `pvlib` for irradiance, `cdsapi` for ERA5, `open-mastr` for the DE
registry, etc.).

Verify the console scripts resolve:

```bash
python -m pricemodeling status   # ETL
weathergen --help                # step ii
demand-model --help              # step iii
res-model --help                 # step iv
avail-model --help               # step v
dispatch-model --help            # steps vi + vii
```

## 3. Datasets & credentials

Copy `.env.example` to `.env` and fill in the secrets, then create a CDS API key file. Each row below
lists what to obtain and the command that ingests it.

| Source | What / why | Credential | Ingest command |
|---|---|---|---|
| **Météo-France SYNOP** | hourly station weather (fits `weathergen`) | none (open) | `python -m pricemodeling extract-meteo` |
| **RTE** `data.rte-france.com` | FR generation per unit/type, load, hydro stock, capacities | OAuth `RTE_CLIENT_ID` / `RTE_CLIENT_SECRET` in `.env` | `python -m pricemodeling extract-rte` |
| **ENTSO-E Transparency** | zonal day-ahead prices, load, generation, flows (6 zones) | `ENTSOE_TOKEN` in `.env` | `python -m pricemodeling extract-entsoe` |
| **ENTSO-E REMIT** | generation-unit unavailability (calibrates step v, feeds dispatch) | same `ENTSOE_TOKEN` | `python -m pricemodeling ingest-remit` |
| **ERA5 / CDS** (incl. ARCO point time-series) | 100 m wind + irradiance (res + weathergen 100 m) | CDS key in `~/.cdsapirc` | pulled on demand by `weathergen`/`res-model` |
| **CMIP6 / CDS** | climate-change quantile deltas (weathergen trend) | same CDS key | `weathergen fetch-cmip6-deltas` |
| **MaStR** (BNetzA) | DE plant-level registry: CHP, retirements → dispatch DE stack | none (bulk download) | `pricemodeling.registries.mastr` ETL |
| **ODRE / OPSD / REPD** | FR + other plant registries (RES bid stack vintages) | none (open) | `pricemodeling.registries.*` |
| **TYNDP** (ENTSO-E/ENTSOG) | 2030–2050 capacity/demand trajectories | none — **hand-entered** into `scenarios.xlsx` (`dispatch_tyndp` tab) | — |

**Credentials setup:**

- **RTE**: create an account at <https://data.rte-france.com>, create an *application*, **subscribe it to
  each API** you need (see `config/rte_catalog.yaml`), then put its `client_id` / `client_secret` in
  `.env`. Verify with `python -m pricemodeling rte-token`.
- **ENTSO-E**: free account at <https://transparency.entsoe.eu>, then email `transparency@entsoe.eu` to
  request API access; paste the token into `.env` as `ENTSOE_TOKEN`.
- **CDS (ERA5/CMIP6)**: account at <https://cds.climate.copernicus.eu>, accept the dataset licences, and
  create `~/.cdsapirc` with your URL + key (the standard `cdsapi` credentials file).

> **Reproducibility caveat.** Raw RTE/ENTSO-E re-pulls are **not bit-identical** over time — those
> operators revise published history. The pipeline is idempotent and cached (re-running only fetches
> what's missing), but a rebuild months later may differ slightly from a prior one. Archive
> `data/pricemodeling.db`, the ERA5 cache, and `simulation.nc` if you need an exact frozen input set
> (see `data/RAW_EXTRACTS_MANIFEST.json`). This is discussed in [REVIEW.md](../REVIEW.md) §9.

## 4. Run the pipeline

### Stage A — build the data base (`pricemodeling`)

```bash
python -m pricemodeling init-db          # create data/pricemodeling.db
python -m pricemodeling extract-meteo    # SYNOP 2014 → today
python -m pricemodeling extract-rte      # all subscribed RTE resources
python -m pricemodeling extract-entsoe   # ENTSO-E prices/load/gen/flows (6 zones)
python -m pricemodeling ingest-remit     # REMIT unavailability
python -m pricemodeling reconcile-units  # EIC-keyed production-unit reconciliation
python -m pricemodeling build-master     # → the hourly master_hourly table
# …or everything: python -m pricemodeling all
python -m pricemodeling status           # summary of the tables
```

### Stage B — fit & simulate the upstream models

```bash
# step ii — weather cube
weathergen -c weathergen/config.yaml fit
weathergen -c weathergen/config.yaml simulate     # → weathergen/output/simulation.nc

# steps iii / iv / v — load, RES, availability (calibrate once, then project)
demand-model -c demand_model/config.yaml   calibrate && demand-model -c demand_model/config.yaml   project
res-model    -c res_model/config.yaml       calibrate && res-model    -c res_model/config.yaml       project
avail-model  -c availability_model/config.yaml calibrate && avail-model -c availability_model/config.yaml project
```

Each model reads the shared `scenarios.xlsx` for its assumptions and writes to the Parquet lake.

### Stage C — dispatch & prices (`dispatch_model`, steps vi + vii)

```bash
# historical backtest (scores model prices vs observed ENTSO-E spot) — the acceptance gate
dispatch-model -c dispatch_model/config.yaml backtest --year 2019

# projection run (future year → per-zone annual price stats)
dispatch-model -c dispatch_model/config.yaml run --year 2030
```

The SMC→spot markup is fitted from the backtest panel and applied automatically in projection; see
`dispatch_model/STEP_VII_METHODOLOGY.md`.

### Fast smoke path

For a quick end-to-end check without the full multi-decade run: use short windows / few draws
(`--draws` on the model CLIs, short date ranges on `pricemodeling extract-*` and
`pricemodeling build-master --start … --end …`, and the `n_weeks`-limited backtest exercised in the
dispatch tests). The per-package test suites (`pytest -q` in each package) run fully offline on synthetic
data in minutes.

## 5. Verify the install

```bash
ruff check .                    # clean
pytest -q                       # (run per package, or the whole tree) — all green
python tools/golden.py check    # "numerically identical to baseline"
python scripts/build_docs.py    # renders the full API reference into docs/api/
```
