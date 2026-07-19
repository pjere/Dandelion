# CONVENTIONS.md — naming & code standards (§4)

Enforced by `ruff` (pycodestyle + pep8-naming) once wired into CI; the domain glossary lives in code at
`powersim_core/glossary.py` (single source of truth).

## Casing (PEP 8)
`snake_case` functions/variables/modules · `PascalCase` classes · `UPPER_SNAKE` constants. No abbreviations
except the whitelist in `glossary.GLOSSARY_ABBREVIATIONS` (cf, mw, mwh, ghi, utc, ntc, srmc, psp, ror, …).

## Domain glossary — canonical names (identical in code, DataFrame/DB columns, config keys, filenames)
| concept | canonical | replaces |
|---|---|---|
| UTC timestamp | `timestamp_utc` (tz-aware UTC, always) | `ts_utc`, `time`, `datetime` |
| identifiers | `scenario_id`, `draw_id`, `year`, `zone`, `region`, `unit_id`, `technology`, `border` | |
| power/energy | `load_mw`, `available_mw`, `production_mw`, `capacity_mw`, `inflow_mwh` | bare `value` |
| weather | `temperature_c`, `wind_speed_ms`, `ghi_wm2` | `temp`, `t2m` |
| dimensionless / price | `cf`, `price_eur_mwh` | |

**Rule:** every physical quantity carries its unit suffix (`_mw`, `_mwh`, `_c`, `_ms`, `_wm2`, `_eur_mwh`).
Never a bare `value`, `data`, `temp`, `df2`.

## Verb semantics (same meaning in every model — `glossary.VERB_SEMANTICS`)
`load_*` read · `build_*` construct in memory · `fit_*`/`calibrate_*` estimate · `project_*`/`generate_*`
forward/stochastic · `validate_*` check acceptance · `write_*` persist.

## Migration
Mechanical renames via LSP/rope (not sed), one PR per package, golden tests green after each
(`python tools/golden.py check`). Legacy timestamp columns are bridged by `glossary.canonical_timestamp`
during transition, then removed.
