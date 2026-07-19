"""Orchestration entry points. Phases 1-7 fill these in (io/transfer/conversion/calibration/
stochastic/projection/validation)."""
from __future__ import annotations

from .config import Config


def calibrate(config: Config):
    """Recalibrate the conversion chains to observed national CFs; save + report acceptance."""
    from .calibration import calibrate_res
    cal = calibrate_res(config)
    out = cal.save(config.models_dir / "calibrated_res.json")
    m = cal.metrics
    print(f"[calibrate] saved -> {out}")
    print("[calibrate] mean CF: " + " | ".join(f"{k} {v*100:.1f}%" for k, v in m["mean_cf"].items()))
    print(f"[calibrate] PV Jul/Dec ratio: {m['pv_jul_dec_ratio']} (target 4-5)")
    print(f"[calibrate] holdout {m['holdout_years']} monthly energy bias %: " +
          " | ".join(f"{k} {v}" for k, v in m["monthly_energy_bias_pct"].items()))
    print(f"[calibrate] hydro ROR leave-one-year-out monthly bias: {m['hydro_loyo_monthly_bias_pct']}% "
          f"(robust; single-year holdout is noisy)")
    print(f"[calibrate] onshore {m['onshore_params']} | offshore {m['offshore_params']}")

    # Phase 5 — fit the stochastic residual layer on (observed − calibrated) CF
    from .stochastic import fit_residual_model
    rm = fit_residual_model(config, cal)
    rout = rm.save(config.models_dir / "residual_res.json")
    rmet = rm.metrics
    print(f"[residual] saved -> {rout}")
    for t in rm.technologies:
        print(f"[residual] {t:14s} resid σ {rmet[f'{t}_resid_std']} | het mid/edge "
              f"{rmet.get(f'{t}_sigma_mid_over_edge')} | AR φ {rmet[f'{t}_phi']} | "
              f"sim/emp std {rmet[f'{t}_std_ratio']}")
    print(f"[residual] cross-tech corr: {rmet['cross_tech_corr']}")
    return cal


def project(config: Config):
    """Project RES production from the weather draws + scenario workbook (coherent with demand)."""
    from .projection import project_all
    return project_all(config)


def run_validation(config: Config):
    """Run the §6 acceptance suite (incl. the demand–RES correlation killer test) + HTML report."""
    from .validation import run_validation_suite
    return run_validation_suite(config)
