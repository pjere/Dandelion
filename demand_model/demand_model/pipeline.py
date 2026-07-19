"""Orchestration entry points. Phases 3-6 fill these in (calibration, projection, validation)."""
from __future__ import annotations

from .config import Config


def calibrate(config: Config):
    """Load history -> features -> fit the additive decomposition -> save + report acceptance."""
    from .calibration.fit import calibrate as _cal
    from .features.build import national_features
    from .io.loaders import load_demand
    from .io.qc import qc_demand

    demand, _ = qc_demand(load_demand(config), config)
    feat = national_features(config)
    model = _cal(config, feat, demand)
    out = model.save(config.models_dir / "calibrated.json")
    m = model.metrics
    print(f"[calibrate] saved -> {out}")
    print(f"[calibrate] thresholds: heat {m['tau_heat']}°C, cool {m['tau_cool']}°C")
    print(f"[calibrate] winter gradient: {m['winter_gradient_gw_per_c']} GW/°C "
          f"(target {config.section('validation')['winter_gradient_gw_per_c']})")
    print(f"[calibrate] MAPE in-sample {m['mape_in_sample']}% | "
          f"holdout {m.get('mape_holdout','n/a')}% | bias {m.get('holdout_bias_pct','n/a')}%")

    # Phase 4 — fit the stochastic residual layer on the mean-model residuals
    _fit_residual(config, model, feat, demand)
    return model


def _fit_residual(config: Config, model, feat, demand):
    """Fit + save the heteroscedastic AR residual model (statistical-core Stage 3)."""
    from .residual import fit_residual_model
    rc = config.section("residual")
    df = feat.join(demand.set_index("timestamp_utc")["load_mw"], how="inner").dropna(
        subset=["load_mw", "T_smooth_60h", "T_smooth_12h"])   # drop all-NaN grid-gap rows
    resid = df["load_mw"] - model.predict(df)                # NaNs (warm-up rows) dropped in fit
    rm = fit_residual_model(resid, order=int(rc.get("ar_order", 2)),
                            min_count=int(rc.get("min_bucket_count", 50)),
                            seed=config.section("run")["seed"])
    rout = rm.save(config.models_dir / "residual.json")
    d = rm.metrics
    print(f"[residual] saved -> {rout}")
    print(f"[residual] σ {d['sigma_min_mw']}–{d['sigma_max_mw']} MW over {d['n_buckets']} buckets "
          f"(global {d['resid_std_mw']} MW) | AR{rm.order} φ={d['phi']}")
    print(f"[residual] lag-1 autocorr emp {d['acf1_empirical']} vs sim {d['acf1_simulated']} | "
          f"std(sim)/std(emp) {d['std_ratio_sim_over_emp']}")


def project(config: Config):
    """Project demand for every scenario from weathergen weather + workbook drivers."""
    from .projection import run_projection
    return run_projection(config)


def run_validation(config: Config):
    """Run the acceptance-check suite and write the HTML validation report."""
    from .validation import run_validation_suite
    return run_validation_suite(config)
