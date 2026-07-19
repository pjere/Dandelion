"""Phase 0 — the modeling-assumptions workbook (§4): template generator + validating loader.

One workbook, tidy long format, same meta/versioning as step (iii). Sheets:
  capacity_trajectories  — technology × region × year → installed MW
  offshore_farms         — farm-by-farm (coords, capacity, commissioning, foundation, turbine class)
  technology_vintages    — technology × cohort-year → descriptors (hub height, specific power, DC/AC…)
  degradation_availability, spatial_distribution, losses  — tidy year/technology sheets
  meta, README
Trajectories here are ILLUSTRATIVE placeholders (PPE-style orders of magnitude) — replace with real
scenario assumptions before using projected levels.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

YEARS = list(range(2025, 2047))
_SCEN = "reference"
PV_SEGMENTS = ["pv_utility", "pv_distributed", "pv_btm"]
WIND_TECHS = ["wind_onshore", "wind_offshore_fixed", "wind_offshore_floating"]


def _lin(y0: float, y1: float) -> list[float]:
    return list(np.round(np.linspace(y0, y1, len(YEARS)), 3))


def _cap_rows(tech: str, region: str, vals: list[float], scenario: str = _SCEN) -> list[dict]:
    return [{"technology": tech, "region": region, "year": y, "capacity_mw": float(v),
             "scenario": scenario} for y, v in zip(YEARS, vals)]


def build_template(path: str | Path) -> Path:
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    sheets: dict[str, pd.DataFrame] = {}

    # --- capacity_trajectories (MW, national region "FR"; offshore split fixed/floating) ---
    cap = []
    cap += _cap_rows("pv_utility", "FR", _lin(9_000, 40_000))
    cap += _cap_rows("pv_distributed", "FR", _lin(7_000, 32_000))
    cap += _cap_rows("pv_btm", "FR", _lin(4_000, 18_000))
    cap += _cap_rows("wind_onshore", "FR", _lin(22_000, 45_000))
    cap += _cap_rows("wind_offshore_fixed", "FR", _lin(1_500, 18_000))
    cap += _cap_rows("wind_offshore_floating", "FR", _lin(0, 5_000))
    sheets["capacity_trajectories"] = pd.DataFrame(cap)

    # --- offshore_farms (illustrative FR AO1–AO9 tenders; coords approximate) ---
    farms = [
        ("Saint-Nazaire", 47.16, -2.62, 480, 2022, "fixed", "GE_Haliade_150_6MW"),
        ("Saint-Brieuc", 48.90, -2.55, 496, 2023, "fixed", "Siemens_SG_8MW"),
        ("Fecamp", 49.90, 0.15, 497, 2024, "fixed", "Siemens_SG_7MW"),
        ("Courseulles-Calvados", 49.35, -0.45, 448, 2025, "fixed", "Siemens_SG_8MW"),
        ("Dieppe-Le_Treport", 50.02, 1.15, 496, 2026, "fixed", "Siemens_SG_8MW"),
        ("Yeu-Noirmoutier", 46.80, -2.35, 496, 2026, "fixed", "GE_Haliade_150_6MW"),
        ("Dunkerque", 51.15, 2.15, 600, 2028, "fixed", "Siemens_SG_11MW"),
        ("Sud-Bretagne_floating", 47.30, -3.60, 250, 2031, "floating", "float_15MW"),
        ("Golfe-du-Lion_floating", 43.15, 3.90, 250, 2031, "floating", "float_15MW"),
    ]
    sheets["offshore_farms"] = pd.DataFrame(
        [{"farm": n, "latitude": la, "longitude": lo, "capacity_mw": c, "commissioning_year": y,
          "foundation": f, "turbine_class": t, "scenario": _SCEN}
         for n, la, lo, c, y, f, t in farms])

    # --- technology_vintages: descriptors by commissioning cohort (illustrative) ---
    vint = []

    def vrow(tech, cohort, var, val):
        vint.append({"technology": tech, "cohort_year": cohort, "variable": var,
                     "value": float(val), "scenario": _SCEN})
    for cohort, hub, sp, upl in [(2010, 80, 400, 0.0), (2020, 100, 320, 0.10), (2035, 125, 270, 0.22)]:
        vrow("wind_onshore", cohort, "hub_height_m", hub)
        vrow("wind_onshore", cohort, "specific_power_w_m2", sp)
        vrow("wind_onshore", cohort, "cf_uplift_vs_legacy", upl)
    for cohort, hub, sp in [(2022, 100, 400), (2030, 140, 330), (2040, 150, 300)]:
        vrow("wind_offshore_fixed", cohort, "hub_height_m", hub)
        vrow("wind_offshore_fixed", cohort, "specific_power_w_m2", sp)
    for cohort, tracker, dcac, tcoef in [(2015, 0.05, 1.15, -0.0040), (2025, 0.30, 1.25, -0.0035),
                                         (2035, 0.55, 1.30, -0.0032)]:
        for seg in ("pv_utility", "pv_distributed"):
            vrow(seg, cohort, "tracker_share", tracker if seg == "pv_utility" else 0.0)
            vrow(seg, cohort, "dc_ac_ratio", dcac)
            vrow(seg, cohort, "tilt_deg", 25 if seg == "pv_distributed" else 20)
            vrow(seg, cohort, "temp_coeff_per_c", tcoef)
    sheets["technology_vintages"] = pd.DataFrame(vint)

    # --- degradation_availability (tidy) ---
    da = [
        {"technology": "pv_utility", "variable": "degradation_pct_per_year", "unit": "pct", "value": 0.5,
         "scenario": _SCEN},
        {"technology": "pv_distributed", "variable": "degradation_pct_per_year", "unit": "pct", "value": 0.5,
         "scenario": _SCEN},
        {"technology": "wind_onshore", "variable": "availability", "unit": "share", "value": 0.96, "scenario": _SCEN},
        {"technology": "wind_offshore_fixed", "variable": "availability", "unit": "share", "value": 0.94,
         "scenario": _SCEN},
        {"technology": "wind_offshore_floating", "variable": "availability", "unit": "share", "value": 0.92,
         "scenario": _SCEN},
        {"technology": "wind_onshore", "variable": "icing_derate", "unit": "share", "value": 0.01, "scenario": _SCEN},
    ]
    sheets["degradation_availability"] = pd.DataFrame(da)

    # --- spatial_distribution (national now: FR share = 1 for every tech) ---
    sd = [{"technology": t, "region": "FR", "variable": "new_capacity_share", "value": 1.0,
           "scenario": _SCEN} for t in PV_SEGMENTS + WIND_TECHS]
    sheets["spatial_distribution"] = pd.DataFrame(sd)

    # --- losses (calibration priors, then fixed at calibrated values) ---
    lo = [
        {"technology": "pv_utility", "variable": "system_loss", "unit": "share", "value": 0.14, "scenario": _SCEN},
        {"technology": "pv_distributed", "variable": "system_loss", "unit": "share", "value": 0.16,
         "scenario": _SCEN},
        {"technology": "wind_onshore", "variable": "wake_electrical_loss", "unit": "share", "value": 0.08,
         "scenario": _SCEN},
        {"technology": "wind_offshore_fixed", "variable": "wake_electrical_loss", "unit": "share", "value": 0.10,
         "scenario": _SCEN},
    ]
    sheets["losses"] = pd.DataFrame(lo)

    sheets["meta"] = pd.DataFrame({
        "key": ["scenario", "version", "author", "date", "notes"],
        "value": [_SCEN, "0.1.0", "", "",
                  "illustrative template — replace with real capacity/vintage trajectories"]})
    sheets["README"] = _readme()

    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name, index=False)
    return path


def _readme() -> pd.DataFrame:
    rows = [
        ("capacity_trajectories", "technology, region, year, capacity_mw", "MW",
         "installed capacity per PV segment / wind tech × region × year"),
        ("offshore_farms", "farm, latitude, longitude, capacity_mw, commissioning_year, foundation, turbine_class",
         "deg / MW / year", "farm-by-farm offshore (sites known from AO1–AO9 tenders)"),
        ("technology_vintages", "technology, cohort_year, variable, value",
         "m / W·m⁻² / share / °C⁻¹",
         "cohort descriptors: hub height, specific power, tracker share, DC/AC, temp coeff, CF uplift"),
        ("degradation_availability", "technology, variable, value", "pct·yr⁻¹ / share",
         "PV degradation; wind availability; icing/high-temp derate"),
        ("spatial_distribution", "technology, region, variable(new_capacity_share), value", "share",
         "regional split of new capacity (national = FR:1 for now)"),
        ("losses", "technology, variable, value", "share",
         "PV system losses; wind wake/electrical losses (calibration priors)"),
        ("meta", "key, value", "-", "scenario name / version / author / date"),
    ]
    return pd.DataFrame(rows, columns=["sheet", "columns", "units", "description"])


# --------------------------------------------------------------------------- #
#  Loader
# --------------------------------------------------------------------------- #
def load_assumptions(path: str | Path) -> dict[str, pd.DataFrame]:
    """Load + validate every sheet. Fails loudly on schema violations or missing sheets."""
    from powersim_core.scenario import load_model_sheets

    from .schemas import CAPACITY_TRAJECTORIES, OFFSHORE_FARMS, TECHNOLOGY_VINTAGES, TIDY_YEAR_SHEET, validate
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"scenarios workbook not found: {path}")
    xl = load_model_sheets(path, "res")                  # this model's tabs from the merged scenarios.xlsx
    required = {"capacity_trajectories", "offshore_farms", "technology_vintages",
                "degradation_availability", "spatial_distribution", "losses", "meta"}
    missing = required - set(xl)
    if missing:
        raise ValueError(f"assumptions workbook missing sheets: {sorted(missing)}")
    schema = {"capacity_trajectories": CAPACITY_TRAJECTORIES, "offshore_farms": OFFSHORE_FARMS,
              "technology_vintages": TECHNOLOGY_VINTAGES, "degradation_availability": TIDY_YEAR_SHEET,
              "spatial_distribution": TIDY_YEAR_SHEET, "losses": TIDY_YEAR_SHEET}
    out: dict[str, pd.DataFrame] = {}
    for name, df in xl.items():
        out[name] = validate(df, schema[name], name) if name in schema else df
    return out
