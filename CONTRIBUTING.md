# Contributing

## Development setup

See [docs/INSTALL.md](docs/INSTALL.md) for the full environment + dataset setup. In short:

```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt
# editable-install the packages in dependency order (see INSTALL.md §2)
pip install -e powersim_core -e pricemodeling -e weathergen \
            -e demand_model -e res_model -e availability_model -e dispatch_model
pre-commit install
```

## The gate — run before every commit

```bash
ruff check .                    # lint (config in pyproject.toml); must be clean
pytest -q                       # per-package suites (run from each package dir, or the whole tree)
python tools/golden.py check    # numerical regression gate; must say "numerically identical"
```

`ruff` and hygiene hooks also run via `pre-commit` on staged files. A red golden check means a change
moved a shipped number — that is either a bug to fix or a **deliberate** re-baseline: only then run
`python tools/golden.py capture` (back up `golden/baseline.json` first) and justify the delta in the
commit message and [REVIEW.md](REVIEW.md).

## Documentation is part of the change (not optional)

**Any behavioural edit to a model must update its documentation in the same commit.** Concretely:

- The **per-function docstrings** are the source of truth for signatures and detail — keep them current;
  they are what the API reference renders (`python scripts/build_docs.py`).
- If you change *what a step does or why*, update [docs/MODELLING.md](docs/MODELLING.md) (the end-to-end
  approach) **and** the relevant package's `METHODOLOGY.md`.
- If you add/move/rename a public module or entry point, update [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
  (the module map) and the package `README.md`.
- Record design decisions in the package `DECISIONS.md` or, if cross-cutting, in [docs/ADR.md](docs/ADR.md).
- A pull request that changes model behaviour without a matching doc change is incomplete.

## Conventions

- Naming, units, and verb semantics: [CONVENTIONS.md](CONVENTIONS.md) (`snake_case`, unit-suffixed columns
  `_mw`/`_eur_mwh`, `timestamp_utc` at boundaries, `load_/build_/fit_/project_/validate_/write_` verbs).
- Randomness goes through `powersim_core.rng` (never `np.random.seed`); output I/O through
  `powersim_core.lake`; fitted models persist through `powersim_core.serialize` (JSON+npz, **never
  pickle**); scenario inputs through `powersim_core.scenario` reading the single `scenarios.xlsx`.
- Cross-package use is **read-only, through the owning package's public API, lazily imported** (ADR-8) —
  never write into another package's store.

## What is and isn't committed

Code, tests, docs, `config/`, `scenarios.xlsx`, `golden/baseline.json`, and `.env.example` are versioned.
Everything a run produces — `data/`, databases, the weather cube, the Parquet lake, per-model
`output/`/`models/`/`.cache/`, generated reports/figures, `docs/api/`, logs, secrets — is git-ignored and
regenerated from the pipeline. See [.gitignore](.gitignore) and [docs/INSTALL.md](docs/INSTALL.md).
