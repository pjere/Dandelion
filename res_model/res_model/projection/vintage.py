"""Phase 6 — vintage-resolved fleet (§2.B).

National production = Σ_cohorts (capacity × cohort-specific CF). New cohorts (bigger hub, lower specific
power for wind; trackers/higher DC-AC for PV) have a higher, flatter CF than the legacy fleet, so the
national CF must not be projected flat. We turn the workbook's capacity trajectory + cohort descriptors
into a per-year **fleet CF multiplier** applied on top of the (current-fleet) calibrated CF:

    fleet_factor(Y) = 1 + Σ_{y≤Y} additions(y)·uplift(cohort(y)) / capacity(Y)

Repowering shows up as net capacity change (retire old + add new at the same site) in the trajectory.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

_PV_TRACKER_UPLIFT = 0.22           # tracker yield gain vs fixed tilt (PV cohort proxy)


def annual_capacity(sheets: dict[str, pd.DataFrame], tech: str, scenario: str = "reference"
                    ) -> pd.Series:
    """Installed capacity (MW) by year for a technology (region-summed)."""
    cap = sheets["capacity_trajectories"]
    d = cap[cap["technology"] == tech]
    if "scenario" in d.columns:
        d = d[d["scenario"] == scenario]
    return d.groupby("year")["capacity_mw"].sum().sort_index()


def _cohort_uplift(sheets: dict[str, pd.DataFrame], tech: str) -> pd.Series:
    """Per-cohort CF uplift vs the legacy fleet (index = cohort_year)."""
    v = sheets["technology_vintages"]
    v = v[v["technology"] == tech]
    if v.empty:
        return pd.Series(dtype=float)
    piv = v.pivot_table(index="cohort_year", columns="variable", values="value", aggfunc="mean")
    if "cf_uplift_vs_legacy" in piv:
        return piv["cf_uplift_vs_legacy"].sort_index()
    if "tracker_share" in piv:                       # PV proxy: trackers lift yield
        return (_PV_TRACKER_UPLIFT * piv["tracker_share"]).sort_index()
    return pd.Series(0.0, index=piv.index)


def fleet_cf_factor(sheets: dict[str, pd.DataFrame], tech: str, years: np.ndarray,
                    scenario: str = "reference") -> pd.Series:
    """Per-year CF multiplier (≥1) capturing the shift to higher-CF cohorts."""
    cap = annual_capacity(sheets, tech, scenario)
    up = _cohort_uplift(sheets, tech)
    if cap.empty or up.empty:
        return pd.Series(1.0, index=years)
    cohort_years = np.array(sorted(up.index))

    def cohort_of(y):                                # latest cohort available by year y
        elig = cohort_years[cohort_years <= y]
        return cohort_years[0] if len(elig) == 0 else elig[-1]

    full = cap.reindex(range(int(cap.index.min()), int(years.max()) + 1)).ffill()
    add = full.diff(); add.iloc[0] = full.iloc[0]; add = add.clip(lower=0.0)
    add_uplift = pd.Series({y: float(up.loc[cohort_of(y)]) for y in add.index})
    factor = {}
    for Y in years:
        m = add.index <= Y
        cap_Y = full.get(Y, full.iloc[-1])
        weighted = float((add[m] * add_uplift[m]).sum())
        factor[Y] = 1.0 + (weighted / cap_Y if cap_Y > 0 else 0.0)
    return pd.Series(factor, index=years)
