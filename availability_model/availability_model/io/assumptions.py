"""Phase 0 — the modeling-assumptions workbook (§5): template generator + validating loader.

Seven sheets, tidy long format, pre-filled with fitted/illustrative defaults so the user only has to
touch the ±10 % forced-outage correction, closures/lifetime extensions and the new-build pipeline.
The `fleet_registry` sheet is DB-grounded (see io/fleet.py). Same meta/versioning as steps (iii)/(iv).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

_SCEN = "reference"


def _p(rows, scenario=_SCEN):
    """Tidy param rows: (key, variable, value, text)."""
    return pd.DataFrame([{"key": k, "variable": v, "value": (val if not isinstance(val, str) else None),
                          "text": (val if isinstance(val, str) else None), "scenario": scenario}
                         for k, v, val in rows])


def build_template(config, path: str | Path) -> Path:
    from .fleet import build_fleet_registry
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    sheets: dict[str, pd.DataFrame] = {}
    sheets["fleet_registry"] = build_fleet_registry(config)

    # --- planned_outage_params (per palier / technology) ---
    planned = []
    for pal, cyc in [("CP0", 12), ("CPY", 12), ("P4", 18), ("P'4", 18), ("N4", 18), ("EPR", 18)]:
        planned += [(pal, "cycle_months", cyc), (pal, "vd_period_years", 10),
                    (pal, "asr_mean_days", 45), (pal, "vp_mean_days", 85), (pal, "vd_mean_days", 180),
                    (pal, "overrun_lognorm_mu", 2.4), (pal, "overrun_lognorm_sigma", 0.7),  # heavy tail (days)
                    (pal, "max_simultaneous", 4)]
    # monthly seasonal placement weights (Apr–Sep heavy) for all nuclear
    wm = {1: .3, 2: .3, 3: .6, 4: 1.3, 5: 1.5, 6: 1.5, 7: 1.4, 8: 1.4, 9: 1.3, 10: .7, 11: .4, 12: .3}
    planned += [("nuclear", f"month_weight_{m:02d}", w) for m, w in wm.items()]
    planned += [("nuclear", "max_simultaneous_fleet", 22), ("thermal", "maint_mean_days", 21),
                ("thermal", "maint_period_years", 2)]
    sheets["planned_outage_params"] = _p(planned)

    # --- forced_outage_params (per technology) — freq + heavy-tailed duration + trend + age ---
    forced = []
    for tech, freq, mu, sig, der in [("nuclear", 3.0, 2.3, 0.9, 0.15), ("gas", 4.0, 1.8, 1.0, 0.20),
                                     ("coal", 5.0, 1.9, 1.0, 0.20), ("oil", 4.0, 1.7, 1.0, 0.20),
                                     ("biomass", 5.0, 1.8, 1.0, 0.15), ("hydro_reservoir", 1.5, 1.5, 0.9, 0.10),
                                     ("hydro_pumped", 2.0, 1.5, 0.9, 0.10), ("hydro_ror", 1.5, 1.5, 0.9, 0.10)]:
        forced += [(tech, "freq_per_unit_year", freq),          # events/unit-yr (baseline, ex-crisis)
                   (tech, "dur_lognorm_mu", mu), (tech, "dur_lognorm_sigma", sig),  # log-days (heavy tail)
                   (tech, "derating_share", der),               # fraction of events that are partial
                   (tech, "trend_slope_pct_yr", 0.5),           # fitted %/yr frequency creep (calendar)
                   (tech, "user_correction_pct", 0.0),          # ±10 % on the trend SLOPE (D2)
                   (tech, "age_creep_pct_yr", 0.3)]             # frequency creep with unit age
    sheets["forced_outage_params"] = _p(forced)

    # --- common_mode (generic-fault events across paliers) — the price-tail driver ---
    cm = [("nuclear", "event_freq_per_year", 0.05),             # ~1-in-20-yr generic event
          ("nuclear", "affected_fraction_mean", 0.6), ("nuclear", "affected_fraction_sd", 0.2),
          ("nuclear", "per_unit_extra_days_mean", 120), ("nuclear", "per_unit_extra_days_sd", 60),
          ("nuclear", "stagger_weeks_mean", 6),                 # inspections cascade, not simultaneous
          ("nuclear", "target_prob_CPY", 0.3), ("nuclear", "target_prob_P4", 0.25),
          ("nuclear", "target_prob_P'4", 0.25), ("nuclear", "target_prob_N4", 0.15),
          ("nuclear", "target_prob_CP0", 0.05)]
    sheets["common_mode"] = _p(cm)

    # --- weather_derating (river-cooled units, per basin) ---
    der = []
    for basin in ["Rhône", "Loire", "Garonne", "Moselle", "Seine", "Meuse", "Vienne"]:
        der += [(basin, "air_temp_threshold_c", 25.0),          # basin water-temp proxy knee
                (basin, "derate_frac_per_c", 0.03),             # fraction of unit capacity lost per °C above
                (basin, "water_lag_weeks", 1.5), (basin, "regulatory_limit_on", 1.0)]
    sheets["weather_derating"] = _p(der)

    # --- interconnectors (pseudo-units; NTC + availability) ---
    ic = []
    for border, imp, exp in [("BE", 4300, 4300), ("DE", 4800, 4800), ("CH", 3700, 1300),
                             ("IT", 4350, 2650), ("ES", 3300, 3500), ("GB", 4000, 4000)]:
        ic += [{"border": border, "direction": "import", "ntc_mw": imp, "planned_unavail": 0.03,
                "forced_unavail": 0.02, "scenario": _SCEN},
               {"border": border, "direction": "export", "ntc_mw": exp, "planned_unavail": 0.03,
                "forced_unavail": 0.02, "scenario": _SCEN}]
    sheets["interconnectors"] = pd.DataFrame(ic)

    # --- hydro_inflows ---
    hy = [("reservoir", "energy_capacity_gwh", 8500), ("reservoir", "inflow_precip_sens", 0.5),
          ("reservoir", "inflow_snowmelt_amp", 0.25), ("reservoir", "inflow_memory_weeks", 8),
          ("ror", "energy_capacity_gwh", 0), ("ror", "inflow_precip_sens", 0.6),
          ("pumped", "cycle_efficiency", 0.75)]
    sheets["hydro_inflows"] = _p(hy)

    sheets["meta"] = pd.DataFrame({"key": ["scenario", "version", "author", "date", "notes"],
                                   "value": [_SCEN, "0.1.0", "", "",
                                             "pre-filled defaults; outage params fitted in Phase 2. "
                                             "User owns ±10% correction, closures, new builds."]})
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name, index=False)
    return path


# --------------------------------------------------------------------------- #
def load_assumptions(path: str | Path) -> dict[str, pd.DataFrame]:
    """Load + validate every sheet. Fails loudly on schema violations or missing sheets."""
    from powersim_core.scenario import load_model_sheets

    from .schemas import FLEET_REGISTRY, INTERCONNECTORS, PARAM_SHEET, validate
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"scenarios workbook not found: {path}")
    xl = load_model_sheets(path, "avail")                # this model's tabs from the merged scenarios.xlsx
    required = {"fleet_registry", "planned_outage_params", "forced_outage_params", "common_mode",
                "weather_derating", "interconnectors", "hydro_inflows", "meta"}
    missing = required - set(xl)
    if missing:
        raise ValueError(f"assumptions workbook missing sheets: {sorted(missing)}")
    param = {"planned_outage_params", "forced_outage_params", "common_mode", "weather_derating",
             "hydro_inflows"}
    out: dict[str, pd.DataFrame] = {}
    for name, df in xl.items():
        if name == "fleet_registry":
            out[name] = validate(df, FLEET_REGISTRY, name)
        elif name == "interconnectors":
            out[name] = validate(df, INTERCONNECTORS, name)
        elif name in param:
            out[name] = validate(df, PARAM_SHEET, name)
        else:
            out[name] = df
    return out
