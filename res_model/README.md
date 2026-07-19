# res_model (step iv) — weather-to-power conversion

Calibrated conversion of the step (ii) synthetic weather draws into hourly **potential** production
(before market curtailment) for solar PV, onshore wind, offshore wind and run-of-river hydro, under
exogenous capacity scenarios. Consumes the **same** weather draws as the demand model (step iii) so
demand↔RES correlations survive into the dispatch/price steps.

Hybrid design: physical conversion chains (pvlib PV; wind power curves) whose parameters are
**statistically recalibrated** against historical production to reproduce observed national capacity
factors and their distributions.

## CLI

```
res-model init-workbook     # write the §4 assumptions workbook template
res-model calibrate         # calibrate the conversion chains on history      (Phase 4)
res-model project           # project production from weather draws + scenarios (Phase 6)
res-model validate          # acceptance checks + report                       (Phase 7)
```

Run from the `res_model/` directory (config defaults to `config.yaml`), e.g.
`python -m res_model.cli init-workbook`.

## Layout

```
res_model/
├─ config.yaml              # single source of truth (paths, series keys, bands)
├─ DECISIONS.md             # decision log (blocking choices D1–D4 fixed with user)
├─ res_model/
│  ├─ config.py  meta.py  pipeline.py  cli.py
│  ├─ io/        # schemas (data contracts), assumptions workbook, loaders (Phase 1)
│  ├─ transfer/  # station→ERA5-100m wind, GHI processing        (Phase 2)
│  ├─ conversion/# pv, wind_onshore, wind_offshore, hydro_ror    (Phase 3)
│  ├─ calibration/  stochastic/  projection/  validation/        (Phases 4-7)
└─ tests/
```

## Status

Phase 0 (scaffold) complete. See `DECISIONS.md` for the decision log and phase plan.

Key deps (shared with step iii): python ≥ 3.11, pvlib, pandas/numpy/scipy, statsmodels, xarray,
pandera, pydantic, openpyxl, pytest, ruff.
