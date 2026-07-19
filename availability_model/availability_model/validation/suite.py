"""Phase 7 — validation suite (§7 acceptance criteria).

Assembles nuclear daily availability for a set of draws (reusing the projection engine) and checks the
behaviours that make the availability model fit for the price step: the non-crisis energy-availability
factor sits in the historical band; common-mode draws reproduce a 2022-magnitude annual trough while
quiet draws don't; the generic-event return period matches the target; planned outages are summer-heavy;
and available capacity is physically bounded. PASS = hard requirement met, WARN = soft/among draws.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..calibration.model import CalibratedAvailability
from ..config import Config
from ..projection.common_mode import simulate_common_mode
from ..projection.engine import _assemble_draw, _horizon, load_scenario_registry
from ..projection.planned_scheduler import planned_metrics, schedule_planned


def _nuclear_daily_kd(config, model, registry, draw, temp, hstart, days, nd, nuc_mask, nuc_cap):
    avail, _, _ = _assemble_draw(config, model, registry, draw, temp, hstart, days, nd)
    return pd.Series(avail[nuc_mask].sum(axis=0) / nuc_cap, index=days)


def run(config: Config, n_draws: int = 15) -> dict:
    from ..io.weather import load_national_weather
    model = CalibratedAvailability.load(config.models_dir / "calibrated_availability.json")
    registry = load_scenario_registry(config)
    temp, _ = load_national_weather(config)
    hstart, days, nd = _horizon(config)
    nuc_mask = ((registry["technology"] == "nuclear") & registry["closure_year"].isna()).to_numpy()
    nuc_cap = registry.loc[nuc_mask, "capacity_mw"].sum()
    vband = config.section("validation")

    rows = []
    per_draw = []
    for d in range(n_draws):
        kd = _nuclear_daily_kd(config, model, registry, d, temp, hstart, days, nd, nuc_mask, nuc_cap)
        worst_annual = kd.rolling(365, min_periods=200).mean().min()
        has_cm = not simulate_common_mode(config, model, registry, draw=d).empty
        per_draw.append({"draw": d, "mean_kd": float(kd.mean()), "worst_annual_kd": float(worst_annual),
                         "common_mode": has_cm})
    pdf = pd.DataFrame(per_draw)
    quiet = pdf[~pdf["common_mode"]]
    event = pdf[pdf["common_mode"]]

    def add(name, status, detail):
        rows.append({"check": name, "status": status, "detail": detail})

    # C1 — non-crisis Kd in the historical band (use quiet draws; fall back to all)
    ref = quiet if len(quiet) else pdf
    kd_nc = float(ref["mean_kd"].mean())
    band = vband["nuclear_kd_noncrisis"]
    add("noncrisis_nuclear_Kd", "PASS" if band["low"] <= kd_nc <= band["high"] else "FAIL",
        f"{kd_nc:.3f} vs [{band['low']}, {band['high']}]")

    # C2 — a common-mode draw reproduces a 2022-magnitude annual trough
    if len(event):
        worst = float(event["worst_annual_kd"].min())
        target = vband["nuclear_kd_crisis_2022"]
        add("common_mode_crisis_trough", "PASS" if worst <= target + 0.06 else "FAIL",
            f"worst annual Kd {worst:.3f} vs ~{target} target (2022)")
    else:
        add("common_mode_crisis_trough", "WARN", "no common-mode event among validation draws")

    # C3 — quiet draws show no false crisis
    if len(quiet):
        q = float(quiet["worst_annual_kd"].min())
        add("quiet_draws_no_false_crisis", "PASS" if q > 0.66 else "WARN",
            f"worst annual Kd on quiet draws {q:.3f} (should stay > ~0.66)")

    # C4 — common-mode return period in the target band
    ret = 1.0 / float(model.common_mode["event_freq_per_year"])
    rb = vband["common_mode_return_years"]
    add("common_mode_return_period", "PASS" if rb["low"] <= ret <= rb["high"] else "FAIL",
        f"{ret:.1f} yr vs [{rb['low']}, {rb['high']}]")

    # C5 — planned outages are summer-heavy (winter scarcity preserved)
    pm = planned_metrics(config, schedule_planned(config, model, registry, draw=0), registry)
    s = pm["seasonality_check"]
    summer, winter = np.mean([s[m] for m in (6, 7, 8, 9)]), np.mean([s[m] for m in (12, 1, 2)])
    add("planned_summer_seasonality", "PASS" if summer > winter else "FAIL",
        f"summer {summer:.2f} vs winter {winter:.2f}; planned unavail {pm['planned_unavailability']:.3f}")

    # C6 — availability is physically bounded (structural)
    kd_all = pdf["mean_kd"]
    add("availability_bounded", "PASS" if (kd_all.between(0, 1)).all() else "FAIL",
        f"mean Kd across draws in [{kd_all.min():.3f}, {kd_all.max():.3f}]")

    # C7 — weather coupling active: derating occurs and lands in summer (soft)
    from ..projection.derating import thermal_derating
    der = thermal_derating(config, model, registry, temp)
    hot_ok = (not der.empty) and pd.DatetimeIndex(der["day"]).month.isin([6, 7, 8, 9]).mean() > 0.8
    add("weather_derating_coupling", "PASS" if hot_ok else "WARN",
        f"{len(der)} derated unit-days, summer-concentrated={hot_ok}")

    npass = sum(r["status"] == "PASS" for r in rows)
    nfail = sum(r["status"] == "FAIL" for r in rows)
    return {"checks": rows, "summary": {"pass": npass, "warn": sum(r["status"] == "WARN" for r in rows),
            "fail": nfail}, "per_draw": per_draw,
            "metrics": {"noncrisis_Kd": kd_nc, "n_draws": n_draws,
                        "n_event_draws": int(len(event)), "return_years": ret}}
