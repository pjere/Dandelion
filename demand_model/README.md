# demand_model — long-term hourly power-demand model (mainland France)

Step (iii) of the price-modelling chain. **Hybrid** statistical–structural model (not a black-box
ML extrapolation): a statistical core calibrated on history + a structural projection layer driven
by scenario assumptions + bottom-up new-load modules + a stochastic residual layer.

> **Status: Phase 0 scaffold.** Config, data contracts (pandera), the assumptions workbook, and the
> CLI wiring are in place and tested. Calibration/projection/validation land phase-by-phase.
> See `DECISIONS.md` for perimeter and modelling choices.

## Install & smoke
```bash
cd demand_model
pip install -e .            # + ".[calib,viz,dev]" for the full stack
pytest -q
```

## Usage
```bash
demand-model init-workbook          # write the assumptions.xlsx template
demand-model calibrate              # fit the statistical core on history (Phase 3)
demand-model project                # project from weather draws + scenario (Phase 5)
demand-model validate               # validation report (Phase 6)
```

## Perimeter (fixed — see DECISIONS.md)
Demand = RTE **REALISED consommation − pumping** (pumping is a dispatch decision → step vi),
losses embedded, **gross demand + explicit BTM-PV netting**, **hourly** (15-min aggregated).
Irradiance derived as clear-sky × cloud (shared with step iv). Multi-scenario workbook.

## Layout
`io/` (schemas + loaders + assumptions workbook) · `features/` (calendar, effective temperature,
irradiance) · `calibration/` · `projection/` · `stochastic/` · `validation/` · `cli.py`.
Single `config.yaml`; outputs partitioned Parquet `scenario/draw/year` with provenance metadata.
