"""DM Phase 5 offline tests: driver factors are anchored, bottom-up conserves energy, PV nets
only incremental capacity."""
from __future__ import annotations

import numpy as np
import pandas as pd
from demand_model.calibration.design import make_design
from demand_model.calibration.model import CalibratedModel
from demand_model.config import load_config
from demand_model.projection.bottomup import btm_pv_netting, ev_load
from demand_model.projection.drivers import Drivers
from demand_model.projection.engine import deterministic_net


def _tidy(rows, scenario="reference"):
    out = []
    for var, unit, series in rows:
        for y, v in series.items():
            out.append({"year": y, "variable": var, "unit": unit, "value": float(v), "scenario": scenario})
    return pd.DataFrame(out)


def _sheets(years):
    def lin(a, b):
        return dict(zip(years, np.linspace(a, b, len(years))))
    sheets = {
        "demography": _tidy([("population", "persons", lin(66e6, 70e6))]),
        "macro": _tidy([("gdp_index", "idx", lin(100, 130)), ("steel_index", "idx", lin(100, 100))]),
        "residential_tertiary": _tidy([
            ("heat_pump_stock", "u", lin(4e6, 16e6)),
            ("hp_cop_avg", "r", lin(3.0, 3.0)),
            ("resistance_heating_stock", "u", lin(9e6, 3e6)),
            ("ac_penetration", "s", lin(0.25, 0.50)),
            ("renovation_specific_demand_index", "idx", lin(100, 100)),
            ("floor_area_index", "idx", lin(100, 110)),
        ]),
        "mobility": _tidy([
            ("ev_fleet_cars", "u", lin(1e6, 20e6)), ("ev_fleet_lcv", "u", lin(0, 0)),
            ("ev_fleet_hgv", "u", lin(0, 0)), ("km_per_car_year", "km", lin(12000, 12000)),
            ("kwh_per_km_car", "k", lin(0.18, 0.18)), ("smart_charging_share", "s", lin(0.3, 0.7)),
        ]),
        "new_large_loads": _tidy([
            ("electrolysis_capacity", "GW", lin(0, 10)), ("electrolysis_load_factor", "s", lin(0.5, 0.5)),
            ("datacentre_load", "GW", lin(1, 5)), ("other_pointload", "GW", lin(0, 0)),
        ]),
        "efficiency": _tidy([("autonomous_efficiency_rate", "f", lin(0.005, 0.005))]),
        "btm_pv": _tidy([("btm_pv_capacity", "GW", lin(5, 45)), ("self_consumption_ratio", "s", lin(0.5, 0.5))]),
        "profiles": pd.DataFrame([{"hour": h, "profile": p, "value": 1.0 / 24}
                                  for p in ("home_evening", "smart_offpeak") for h in range(24)]),
    }
    return sheets


def _drivers(anchor=2026):
    years = np.arange(2027, 2047)
    sheets = _sheets(np.arange(2025, 2047))
    return Drivers(sheets, "reference", anchor, {"residential": 0.45, "tertiary": 0.4, "industry": 0.15}, years)


def test_factors_grow_and_efficiency_bites():
    d = _drivers()
    f = d.component_factors()
    # base grows with population/GDP but is tempered by efficiency erosion (< raw structural growth)
    assert f["base"].iloc[-1] > 1.0
    assert f["cool"].iloc[-1] > f["cool"].iloc[0]                 # AC penetration doubling
    # heating: HP electrification adds electric heating sensitivity — with the COLD-weather COP (heating
    # is drawn when the HP COP has collapsed) it roughly balances renovation/efficiency, so the factor
    # stays near 1 rather than collapsing (keeps the winter gradient robust). Anchor-normalised → ==1 at anchor.
    assert 0.7 < f["heat"].iloc[-1] < 1.4
    assert np.isfinite(f.to_numpy()).all()


def test_ev_energy_conserved_and_shaped():
    d = _drivers()
    idx = pd.date_range("2035-01-01", periods=8760, freq="h", tz="UTC")
    ev = ev_load(d, idx, {"ev_segments": {"car": {"km_per_year_var": "km_per_car_year",
                                                  "kwh_per_km_var": "kwh_per_km_car"}}})
    # recompute expected annual energy from the interpolated drivers (~linear interp at 2035: km 12000, kWh/km 0.18)
    exp_kwh = float(d.at("mobility", "ev_fleet_cars").loc[2035]) * 12000 * 0.18
    got_mwh = ev.sum()                                            # MW·h == MWh
    assert abs(got_mwh * 1000 - exp_kwh) / exp_kwh < 0.02         # energy conserved ~2%
    assert (ev >= 0).all() and ev.std() > 0                       # has an intraday shape


def test_pv_nets_only_incremental():
    d = _drivers()
    # incremental capacity is 0 at the anchor and grows with the horizon
    incr = (d.series("btm_pv", "btm_pv_capacity") - d.series("btm_pv", "btm_pv_capacity").loc[2026])
    assert incr.loc[2026] == 0.0 and incr.loc[2046] > incr.loc[2027]
    day = np.r_[np.zeros(6), np.linspace(0, 800, 12), np.zeros(6)]
    early = pd.Series(day, index=pd.date_range("2027-06-01", periods=24, freq="h", tz="UTC"))
    late = pd.Series(day, index=pd.date_range("2046-06-01", periods=24, freq="h", tz="UTC"))
    pv_e, pv_l = btm_pv_netting(d, early, 0.8), btm_pv_netting(d, late, 0.8)
    assert (pv_e >= 0).all() and pv_e.max() > 0                   # daytime self-consumption
    assert pv_l.sum() > 5 * pv_e.sum()                           # late-horizon PV nets much more


def _synthetic_proj_feat(year=2035, n=24 * 20):
    idx = pd.date_range(f"{year}-01-15", periods=n, freq="h", tz="UTC")
    t = 5 + 3 * np.sin(np.arange(n) / 24)
    feat = pd.DataFrame(index=idx)
    feat["T_nat"] = t
    feat["T_smooth_12h"] = pd.Series(t, index=idx).ewm(halflife=12).mean()
    feat["T_smooth_60h"] = pd.Series(t, index=idx).ewm(halflife=60).mean()
    feat["T_lag_d1"] = t; feat["T_lag_d2"] = t
    feat["hour_local"] = idx.hour; feat["dow"] = idx.dayofweek; feat["month"] = idx.month
    feat["day_type"] = np.where(idx.dayofweek >= 5, "sat", "tue_thu")
    feat["school_frac"] = 0.0
    feat["GHI_nat"] = np.clip(400 * np.sin((idx.hour - 6) / 12 * np.pi), 0, None)
    feat["trend_years"] = 11.0; feat["is_covid"] = 0.0; feat["is_sobriety"] = 0.0
    feat.index.name = "timestamp_utc"
    return feat


def test_deterministic_net_assembly_and_driver_scaling():
    cfg = load_config("config.yaml")
    feat = _synthetic_proj_feat()
    X, groups = make_design(feat, 15.0, 20.0)
    model = CalibratedModel(intercept=40000.0, coef=pd.Series(0.0, index=X.columns), groups=groups,
                            tau_heat=15.0, tau_cool=20.0, tau_cold=2.0, halflives_h=[12, 60])
    years = np.arange(2025, 2047)
    net, parts = deterministic_net(cfg, model, feat, _sheets(years), "reference")
    assert np.isfinite(net.to_numpy()).all() and (net > 0).all()
    # bottom-up + base present; EV and new loads add positive energy
    assert parts["ev"].sum() > 0 and parts["new_loads"].sum() > 0 and (parts["btm_pv"] <= 0).all()
    # stronger post-anchor population growth lifts the base-driven load (factor is anchor-relative)
    sheets2 = _sheets(years)
    post = sheets2["demography"]["year"] > 2026
    sheets2["demography"].loc[post, "value"] *= 1.5
    net2, _ = deterministic_net(cfg, model, feat, sheets2, "reference")
    assert net2.mean() > net.mean()
