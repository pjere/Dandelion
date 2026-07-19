"""AVAIL Phase 5 — weather derating, reservoir energy budget, interconnector availability."""
from __future__ import annotations

import numpy as np
import pandas as pd
from availability_model.projection.derating import thermal_derating
from availability_model.projection.hydro import reservoir_energy_budget
from availability_model.projection.interconnectors import interconnector_availability


def _temp_with_heatwave(cfg):
    y0 = cfg.section("projection")["horizon"]["start_year"]
    y1 = cfg.section("projection")["horizon"]["end_year"]
    days = pd.date_range(f"{y0}-01-01", f"{y1}-12-31", freq="D", tz="UTC")
    t = 12 - 9 * np.cos(2 * np.pi * days.dayofyear.to_numpy() / 365)      # seasonal cycle
    hw = (days.month == 8) & (days.day <= 10)                            # August heat wave
    return pd.Series(np.where(hw, t + 12, t), index=days)


def test_derating_hits_river_not_sea(cfg, model, registry):
    d = thermal_derating(cfg, model, registry, _temp_with_heatwave(cfg))
    assert not d.empty
    assert (d["avail_frac"] > 0).all() and (d["avail_frac"] < 1).all()
    assert (pd.DatetimeIndex(d["day"]).month.isin([7, 8, 9])).all()      # only hot months derate
    river = registry.loc[(registry["technology"] == "nuclear") & (registry["cooling"] == "river"), "unit_id"]
    sea = registry.loc[(registry["technology"] == "nuclear") & (registry["cooling"] == "sea"), "unit_id"]
    assert d["unit_id"].isin(river).any() and not d["unit_id"].isin(sea).any()
    assert d["avail_frac"].min() >= 1 - 0.30                             # regulatory derate cap


def test_reservoir_budget_dry_year_lower(cfg, model):
    y0 = cfg.section("projection")["horizon"]["start_year"]
    wet = {y: (0.7 if y == y0 + 8 else 1.0) for y in range(y0, y0 + 20)}
    rb = reservoir_energy_budget(cfg, model, wetness_by_year=wet)
    floor = model.inflows["reservoir"]["min_stock_gwh"]
    cap = model.inflows["reservoir"]["energy_capacity_gwh"]
    assert (rb["avail_energy_gwh"] >= floor - 1).all() and (rb["avail_energy_gwh"] <= cap * 1.5).all()
    dry = rb[rb["week_start"].dt.year == y0 + 8]["avail_energy_gwh"].mean()
    norm = rb[rb["week_start"].dt.year == y0 + 7]["avail_energy_gwh"].mean()
    assert dry < norm                                                   # dry year lowers the ceiling


def _ic_df():
    rows = []
    for b, imp, exp in [("BE", 4300, 4300), ("DE", 4800, 4800), ("ES", 3300, 3500)]:
        rows += [{"border": b, "direction": "import", "ntc_mw": imp, "planned_unavail": 0.03, "forced_unavail": 0.02},
                 {"border": b, "direction": "export", "ntc_mw": exp, "planned_unavail": 0.03, "forced_unavail": 0.02}]
    return pd.DataFrame(rows)


def test_interconnector_availability(cfg):
    ic = _ic_df()
    a = interconnector_availability(cfg, ic, draw=0)
    assert (a["available_ntc_mw"] >= 0).all()
    for (b, dr), g in a.groupby(["border", "direction"]):
        ntc = ic[(ic["border"] == b) & (ic["direction"] == dr)]["ntc_mw"].iloc[0]
        assert g["available_ntc_mw"].max() <= ntc                        # never exceeds NTC
        assert 0.90 <= g["available_ntc_mw"].mean() / ntc <= 0.98        # ≈ 1 − planned − forced
    b = interconnector_availability(cfg, ic, draw=0)
    assert a["available_ntc_mw"].equals(b["available_ntc_mw"])          # reproducible
