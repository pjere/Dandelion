"""Phase 3 — build the additive design matrix from the national feature frame.

Groups are kept separable so the projection layer can rescale each component independently:
  base   : day-type×hour shape + month seasonality + school-vacation + linear trend
  heat   : HDD×hour (winter ramp) + HDD×weekend + a cold-tail term
  cool   : CDD×hour (afternoon AC)
  light  : cloud-driven GHI deficit × hour (dark-afternoon lighting)
  anomaly: COVID + sobriety level shifts
Thresholds are recomputed here from the estimated knees (not the cached defaults).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

DAY_TYPES = ["mon", "tue_thu", "fri", "sat", "sun", "holiday", "pont"]


def _special_days(idx: pd.DatetimeIndex) -> pd.DataFrame:
    """Distinct-load special-day flags derived from the timestamps (Europe/Paris dates)."""
    from ..features.calendar import _holidays
    local = idx.tz_convert("Europe/Paris")
    dates = pd.Series(local.normalize().date, index=idx)
    hol = set(_holidays(int(local.year.min()), int(local.year.max())))
    day_before = pd.Series([(d + pd.Timedelta(days=1)) in hol for d in dates], index=idx).astype(float)
    day_after = pd.Series([(d - pd.Timedelta(days=1)) in hol for d in dates], index=idx).astype(float)
    md = local.month * 100 + local.day
    xmas = pd.Series(((md >= 1224) | (md <= 101)).astype(float), index=idx)
    august = pd.Series((local.month == 8).astype(float), index=idx)
    return pd.DataFrame({"day_before_hol": day_before, "day_after_hol": day_after,
                         "xmas_week": xmas, "august": august}, index=idx)


def make_design(feat: pd.DataFrame, tau_heat: float, tau_cool: float, tau_cold: float = 2.0
                ) -> tuple[pd.DataFrame, dict[str, list[str]]]:
    """Return (X, groups). ``feat`` is the national feature frame (must hold T_smooth_* etc.)."""
    idx = feat.index
    hour = feat["hour_local"].astype(int)
    dt = feat["day_type"].astype(str)
    weekend = feat["dow"].isin([5, 6]).astype(float)
    t_slow = feat["T_smooth_60h"]
    t_fast = feat["T_smooth_12h"]

    HDD = np.clip(tau_heat - t_slow, 0, None)
    HDD_cold = np.clip(tau_cold - t_slow, 0, None)
    CDD = np.clip(t_fast - tau_cool, 0, None)
    ghi = (feat["GHI_nat"] / 1000.0)                     # kW/m² scale for ridge balance

    cols: dict[str, pd.Series] = {}
    groups: dict[str, list[str]] = {"base": [], "heat": [], "cool": [], "light": [], "anomaly": []}

    def add(group, name, series):
        cols[name] = series.astype(float).to_numpy()
        groups[group].append(name)

    # --- base: day-type × hour (load shape) ---
    for d in DAY_TYPES:
        dd = (dt == d).to_numpy()
        for h in range(24):
            add("base", f"base_{d}_h{h:02d}", pd.Series(dd & (hour == h).to_numpy(), index=idx))
    # month seasonality (drop Jan as reference), school vacation, trend
    for m in range(2, 13):
        add("base", f"month_{m:02d}", (feat["month"] == m).astype(float))
    add("base", "school_frac", feat["school_frac"])
    add("base", "school_frac_weekday", feat["school_frac"] * (1 - weekend))
    add("base", "trend_years", feat["trend_years"])
    # permanent post-2022 LEVEL step: demand dropped and stayed down (crisis + sobriety +
    # deindustrialisation). A level (not a slope) generalises without extrapolation blow-up.
    yr = idx.year.to_numpy() + (idx.dayofyear.to_numpy() - 1) / 365.25
    add("base", "step_post2022", pd.Series((yr >= 2022.67).astype(float), index=idx))
    # special days (distinct load): day before/after a holiday, Christmas week, August shutdown
    sp = _special_days(idx)
    for name in ("day_before_hol", "day_after_hol", "xmas_week", "august"):
        add("base", name, sp[name])
        for h in range(24):                              # let their daily shape differ too
            add("base", f"{name}_h{h:02d}", sp[name] * (hour == h).to_numpy())
    # seasonal modulation of the daily SHAPE (ramp timing differs winter vs summer)
    winter = feat["month"].isin([12, 1, 2]).to_numpy()
    summer = feat["month"].isin([6, 7, 8]).to_numpy()
    for h in range(24):
        add("base", f"winter_h{h:02d}", pd.Series(winter & (hour == h).to_numpy(), index=idx))
        add("base", f"summer_h{h:02d}", pd.Series(summer & (hour == h).to_numpy(), index=idx))

    # --- heat: slow HDD × hour (weekday/weekend shapes), fast HDD × hour (morning ramp),
    #     lagged-daily-temperature heating (thermal mass), cold tail ---
    HDD_fast = np.clip(tau_heat - t_fast, 0, None)
    for h in range(24):
        add("heat", f"HDD_wd_h{h:02d}", HDD.where((hour == h) & (weekend == 0), 0.0))
        add("heat", f"HDD_we_h{h:02d}", HDD.where((hour == h) & (weekend == 1), 0.0))
        add("heat", f"HDDfast_h{h:02d}", HDD_fast.where(hour == h, 0.0))
    add("heat", "HDD_cold", HDD_cold)
    if "T_lag_d1" in feat:
        add("heat", "HDD_lag1", np.clip(tau_heat - feat["T_lag_d1"], 0, None))
    if "T_lag_d2" in feat:
        add("heat", "HDD_lag2", np.clip(tau_heat - feat["T_lag_d2"], 0, None))

    # --- cool: CDD × hour ---
    for h in range(24):
        add("cool", f"CDD_h{h:02d}", CDD.where(hour == h, 0.0))

    # --- light: GHI × hour (dark afternoons add lighting load -> expect negative coef) ---
    for h in range(24):
        add("light", f"GHI_h{h:02d}", ghi.where(hour == h, 0.0))

    # --- anomaly level shifts ---
    for a in ("is_covid", "is_sobriety"):
        if a in feat:
            add("anomaly", a, feat[a])

    X = pd.DataFrame(cols, index=idx)
    return X, groups
