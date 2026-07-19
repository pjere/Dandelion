# weathergen

Multi-site **hourly stochastic weather generator** with EVT tails and an externally
imposed climate-change trend. Fits to historical multi-site SYNOP data (from the
`pricemodeling` `master_hourly` / `dim_station` tables) and simulates physically
plausible synthetic weather over 20+ years.

> **Status: Phase 0 scaffold.** The pipeline is fully wired end-to-end on synthetic
> data (smoke test), but the phase modules contain clearly-marked PLACEHOLDER logic.
> Real statistics land phase-by-phase with sign-off. See `DECISIONS.md`.

## Install & smoke test

```bash
cd weathergen
pip install -e .            # core; add: pip install -e ".[stats,viz,dev]" for full stack
pytest -q                  # end-to-end smoke on synthetic data (<1 min)
```

## Usage

```bash
weathergen -c config.yaml fit        # fit + serialize -> models/fitted.pkl
weathergen -c config.yaml simulate   # generate -> output/simulation.nc + validation report
```

## Design

- **Config-driven** (`config.yaml`), **fit-once/simulate-many**, single **seeded** RNG.
- `xarray` cube `(time, station, variable)`; fitted objects serialized to `models/`.
- Phases: `io` (QC) → `climatology` (harmonics) → `transforms` → `marginals` (EVT) →
  `dependence` (copula + EOF-VAR) → `trend` (QDM) → `simulate` → `validate`.

## Non-negotiables (enforced as phases land)

- Trend imposed externally (CMIP6), never estimated from the 12-yr record.
- EVT marginals extrapolate; extremes are not capped at observed maxima.
- Solar excluded (no irradiance in source data); see `DECISIONS.md` D0.2.
- Spell/persistence + inter-station validation are first-class.
- Every clip / imputation / assumption is logged, not hidden.
