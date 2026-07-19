"""Phase 0 — the modeling-assumptions workbook (§6): template generator + validating loader.

One workbook, one sheet per driver family, tidy long format (year, variable, unit, value,
scenario). Every sheet is validated on load; gaps/type errors fail loudly. A README sheet
documents each variable and unit. Hourly shapes (EV charging, intraday profiles) live in a
dedicated ``profiles`` sheet keyed by (hour, profile, value).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .schemas import ASSUMPTION_SHEET, WEIGHTS_SHEET, validate

YEARS = list(range(2025, 2047))                     # scenario horizon
_SCEN = "reference"


def _lin(y0: float, y1: float) -> list[float]:
    return list(np.round(np.linspace(y0, y1, len(YEARS)), 4))


def _tidy(rows: list[tuple[str, str, list[float]]], scenario: str = _SCEN) -> pd.DataFrame:
    out = []
    for var, unit, vals in rows:
        for y, v in zip(YEARS, vals):
            out.append({"year": y, "variable": var, "unit": unit, "value": float(v), "scenario": scenario})
    return pd.DataFrame(out)


def build_template(path: str | Path) -> Path:
    """Write a populated (illustrative) multi-scenario assumptions workbook."""
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    sheets: dict[str, pd.DataFrame] = {}

    sheets["demography"] = _tidy([
        ("population", "persons", _lin(65.8e6, 67.6e6)),
    ])
    sheets["macro"] = _tidy([
        ("gdp_index", "index_2025=100", _lin(100, 135)),
        ("steel_index", "index_2025=100", _lin(100, 110)),
        ("chemicals_index", "index_2025=100", _lin(100, 115)),
        ("cement_index", "index_2025=100", _lin(100, 105)),
        ("paper_index", "index_2025=100", _lin(100, 100)),
    ])
    sheets["residential_tertiary"] = _tidy([
        ("heat_pump_stock", "units", _lin(4.0e6, 16.0e6)),
        ("hp_cop_avg", "ratio", _lin(2.8, 3.4)),
        ("resistance_heating_stock", "units", _lin(9.0e6, 4.0e6)),
        ("ac_penetration", "share_0_1", _lin(0.25, 0.55)),
        ("renovation_specific_demand_index", "index_2025=100", _lin(100, 78)),
        ("floor_area_index", "index_2025=100", _lin(100, 112)),
    ])
    sheets["mobility"] = _tidy([
        ("ev_fleet_cars", "units", _lin(1.5e6, 22.0e6)),
        ("ev_fleet_lcv", "units", _lin(0.2e6, 4.0e6)),
        ("ev_fleet_hgv", "units", _lin(0.01e6, 0.5e6)),
        ("km_per_car_year", "km", _lin(12000, 11000)),
        ("kwh_per_km_car", "kWh/km", _lin(0.18, 0.15)),
        ("smart_charging_share", "share_0_1", _lin(0.2, 0.7)),
    ])
    sheets["new_large_loads"] = _tidy([
        ("electrolysis_capacity", "GW", _lin(0.1, 12.0)),
        ("electrolysis_load_factor", "share_0_1", _lin(0.4, 0.6)),
        ("datacentre_load", "GW", _lin(1.0, 6.0)),
        ("other_pointload", "GW", _lin(0.0, 2.0)),
    ])
    sheets["efficiency"] = _tidy([
        ("autonomous_efficiency_rate", "frac_per_year", _lin(0.006, 0.004)),
    ])
    sheets["btm_pv"] = _tidy([
        ("btm_pv_capacity", "GW", _lin(5.0, 45.0)),
        ("self_consumption_ratio", "share_0_1", _lin(0.55, 0.45)),
    ])

    # hourly shape library (EV charging archetypes; sum to 1 over 24h) — Europe/Paris local hour
    prof = []
    shapes = {
        "home_evening": _bump(19, 4.0), "smart_offpeak": _bump(2, 3.0),
        "workplace": _bump(11, 3.5), "fast_daytime": _bump(14, 5.0),
    }
    for name, s in shapes.items():
        s = s / s.sum()
        for h in range(24):
            prof.append({"hour": h, "profile": name, "value": float(round(s[h], 5))})
    sheets["profiles"] = pd.DataFrame(prof)

    sheets["weights"] = pd.DataFrame({"station_id": ["_FILL_"], "region": ["_FILL_"], "weight": [1.0]})
    sheets["meta"] = pd.DataFrame({
        "key": ["scenario", "version", "author", "date", "notes"],
        "value": [_SCEN, "0.1.0", "", "", "illustrative template — replace with real trajectories"],
    })
    sheets["README"] = _readme()

    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name, index=False)
    return path


def _bump(peak_hour: int, width: float) -> np.ndarray:
    h = np.arange(24)
    d = np.minimum(np.abs(h - peak_hour), 24 - np.abs(h - peak_hour))
    return np.exp(-0.5 * (d / width) ** 2)


def _readme() -> pd.DataFrame:
    rows = [
        ("demography", "population", "persons", "mainland France population by year"),
        ("macro", "gdp_index / <sector>_index", "index (2025=100)",
         "GDP + electro-intensive sector activity → industrial base"),
        ("residential_tertiary",
         "heat_pump_stock / hp_cop_avg / resistance_heating_stock / ac_penetration / "
         "renovation_specific_demand_index / floor_area_index",
         "units / ratio / share / index", "reshapes thermosensitivity + base"),
        ("mobility", "ev_fleet_* / km_per_car_year / kwh_per_km_car / smart_charging_share",
         "units / km / kWh/km / share", "EV energy + charging timing"),
        ("new_large_loads", "electrolysis_capacity / _load_factor / datacentre_load / other_pointload",
         "GW / share", "bottom-up new large loads"),
        ("efficiency", "autonomous_efficiency_rate", "frac/year", "annual efficiency gain on the legacy base"),
        ("btm_pv", "btm_pv_capacity / self_consumption_ratio", "GW / share",
         "behind-the-meter PV netting (uses irradiance draw)"),
        ("profiles", "hour, profile, value", "share (sum=1/24h)",
         "hourly charging archetypes (local Europe/Paris hour)"),
        ("weights", "station_id, region, weight", "share (sum=1)", "station→region consumption/pop weights for T_nat"),
        ("meta", "key, value", "-", "scenario name/version/author/date"),
    ]
    return pd.DataFrame(rows, columns=["sheet", "variables", "units", "description"])


# --------------------------------------------------------------------------- #
#  Loader
# --------------------------------------------------------------------------- #
def load_assumptions(path: str | Path) -> dict[str, pd.DataFrame]:
    """Load + validate every sheet. Fails loudly on schema violations or missing sheets."""
    from powersim_core.scenario import load_model_sheets
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"scenarios workbook not found: {path}")
    xl = load_model_sheets(path, "demand")               # this model's tabs from the merged scenarios.xlsx
    required = {"demography", "macro", "residential_tertiary", "mobility",
                "new_large_loads", "efficiency", "btm_pv", "weights", "meta"}
    missing = required - set(xl)
    if missing:
        raise ValueError(f"assumptions workbook missing sheets: {sorted(missing)}")
    out: dict[str, pd.DataFrame] = {}
    for name, df in xl.items():
        if name in ("weights",):
            out[name] = validate(df, WEIGHTS_SHEET, name)
        elif name in ("meta", "README", "profiles"):
            out[name] = df
        else:
            out[name] = validate(df, ASSUMPTION_SHEET, name)
    return out
