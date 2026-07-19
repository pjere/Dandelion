"""Phase 2 — forced-outage calibration.

Nuclear is fitted from the inferred short-outage catalogue (ex-crisis): frequency per unit-year, a
heavy-tailed duration (lognormal on log-days) and a calendar trend. This counts MULTI-DAY FULL forced
outages only: the `min_outage_days` floor drops brief (<2 d) trips and partial derations are counted as
available, so the fitted frequency sits *below* the nameplate EFOR event count (which includes short
trips) — the literature band is reported for reference, not as a target. What matters for daily
availability (a 6-hour trip barely dents daily energy) is captured; the total unavailability is anchored
by the observed Kd (~0.74), not by this event count.

Non-inferable technologies (gas / oil / coal / biomass / hydro) get literature EFOR-consistent
defaults — production idling there is economic, not unavailability, so nothing can be inferred.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# EFOR-consistent literature defaults for the merit-order fleet (freq events/unit-yr, dur lognormal
# on log-days, ~partial-derating share). Illustrative but standard-order; user-editable in the workbook.
_LIT = {
    "gas":             {"freq_per_unit_year": 4.0, "dur_lognorm_mu": 1.6, "dur_lognorm_sigma": 0.9},
    "oil":             {"freq_per_unit_year": 3.0, "dur_lognorm_mu": 1.4, "dur_lognorm_sigma": 0.9},
    "coal":            {"freq_per_unit_year": 5.0, "dur_lognorm_mu": 1.7, "dur_lognorm_sigma": 0.9},
    "biomass":         {"freq_per_unit_year": 5.0, "dur_lognorm_mu": 1.6, "dur_lognorm_sigma": 0.9},
    "hydro_reservoir": {"freq_per_unit_year": 1.5, "dur_lognorm_mu": 1.4, "dur_lognorm_sigma": 0.8},
    "hydro_pumped":    {"freq_per_unit_year": 2.0, "dur_lognorm_mu": 1.4, "dur_lognorm_sigma": 0.8},
    "hydro_ror":       {"freq_per_unit_year": 1.5, "dur_lognorm_mu": 1.4, "dur_lognorm_sigma": 0.8},
}
_NUCLEAR_EFOR_BAND = (2.0, 5.0)                                     # literature forced events/unit-yr


def _trend_pct_yr(starts: pd.Series) -> float:
    """OLS slope of annual forced-event counts, expressed as %/yr of the mean count."""
    yr = starts.dt.year
    cnt = yr.value_counts().sort_index()
    if len(cnt) < 3 or cnt.mean() == 0:
        return 0.0
    x = cnt.index.to_numpy(float)
    slope = np.polyfit(x - x.mean(), cnt.to_numpy(float), 1)[0]
    return float(round(100 * slope / cnt.mean(), 3))


def _expected_planned_unavail(planned: dict, registry: pd.DataFrame, vd_period_years: float) -> float:
    """Fleet-weighted expected planned unavailability from the calibrated cadence + durations."""
    counts = registry[registry["technology"] == "nuclear"]["palier"].value_counts().to_dict()
    num = den = 0.0
    for palier, p in planned.items():
        n_units = counts.get(palier, 0)
        if not n_units or not p.get("cycle_months"):
            continue
        refuel_yr = 12.0 / p["cycle_months"]
        vd_yr = 1.0 / vd_period_years
        n_asr = (p["types"]["ASR"]["n"] or 1); n_vp = (p["types"]["VP"]["n"] or 1)
        p_asr = n_asr / (n_asr + n_vp)
        mean_refuel = p_asr * (p["types"]["ASR"]["mean_days"] or 0) + (1 - p_asr) * (p["types"]["VP"]["mean_days"] or 0)
        mean_vd = p["types"]["VD"]["mean_days"] or 0
        unavail = (max(0.0, refuel_yr - vd_yr) * mean_refuel + vd_yr * mean_vd) / 365.0
        num += unavail * n_units; den += n_units
    return num / den if den else 0.0


def calibrate_forced(events: pd.DataFrame, observed_unit_days: dict[str, float], config,
                     planned: dict | None = None, registry: pd.DataFrame | None = None,
                     baseline_unavail: float | None = None, planned_unavail_override: float | None = None,
                     forced_share: float | None = None) -> dict:
    out: dict[str, dict] = {}
    # --- nuclear: inferred short/multi-day full events (ex-crisis) ---
    nf = events[(events["technology"] == "nuclear") & (~events["in_crisis"])
                & (events["outage_type"] == "forced")]
    uy = observed_unit_days.get("nuclear", 0.0) / 365.25
    dur = nf["duration_days"].to_numpy()
    lg = np.log(dur[dur > 0])
    freq = float(len(nf) / uy) if uy > 0 else np.nan
    mu, sig = float(lg.mean()), float(lg.std(ddof=1))
    nuc = {
        "freq_per_unit_year": round(freq, 3), "dur_lognorm_mu": round(mu, 3), "dur_lognorm_sigma": round(sig, 3),
        "trend_slope_pct_yr": _trend_pct_yr(nf["start"]),
        "n_events": int(len(nf)), "unit_years": round(uy, 1),
        "source": "inferred_multiday_full", "literature_band_reference": _NUCLEAR_EFOR_BAND,
    }
    # Residual anchoring: the cadence scheduler places routine décennale/refuelling outages, but history
    # also carries extended UNPLANNED outages (long repairs) that the short-forced fit misses. Anchor the
    # nuclear forced day-budget to (baseline unavailability − expected planned cadence) by lengthening the
    # forced duration into a heavy tail — so planned + forced reproduces the observed Kd (~0.74).
    if planned is not None and registry is not None and baseline_unavail is not None and freq > 0:
        vd_years = float(config.section("projection").get("vd_period_years", 10))
        # prefer the MEASURED scheduler planned unavailability (self-consistent) over the analytical est.
        planned_exp = (planned_unavail_override if planned_unavail_override is not None
                       else _expected_planned_unavail(planned, registry, vd_years))
        short_unavail = freq * float(np.exp(mu + sig ** 2 / 2)) / 365.0
        if forced_share is not None:
            # REMIT split (#81): forced is a fixed SHARE of the baseline; the rest is realised as *extended
            # planned* via `planned_duration_mult` (the scheduler lengthens outages). This corrects the old
            # residual-into-forced anchoring, which mislabelled extended scheduled maintenance as forced
            # (~40% vs REMIT's ~10%), while preserving the observed Kd (baseline = planned + forced).
            target = forced_share * baseline_unavail
            planned_target = baseline_unavail - target
            nuc["planned_duration_mult"] = round(planned_target / max(1e-6, planned_exp), 3)
            nuc["forced_share_used"] = forced_share
            source = "remit_share"
        else:
            # union residual, gross-up for suppression: forced starting inside a planned window is dropped,
            # so a gross rate g delivers ≈ g·(1−P) on the open days; solve g so P + g·(1−P) = baseline.
            net_target = max(0.0, baseline_unavail - planned_exp)
            gross = net_target / max(1e-6, 1 - planned_exp)
            target = max(short_unavail, gross)
            source = "residual_anchored"
        mean_dur = target * 365.0 / freq
        nuc["dur_lognorm_mu"] = round(float(np.log(mean_dur) - sig ** 2 / 2), 3)
        nuc.update({"mean_duration_days": round(mean_dur, 1), "planned_unavail_used": round(planned_exp, 3),
                    "forced_gross_unavail": round(target, 3), "baseline_unavail": round(baseline_unavail, 3),
                    "source": source})
    out["nuclear"] = nuc
    # --- non-inferable fleet: literature EFOR ---
    for tech, p in _LIT.items():
        out[tech] = {**p, "trend_slope_pct_yr": 0.5, "source": "literature"}
    return out
