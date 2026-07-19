"""Command-line entry points: ``weathergen fit`` and ``weathergen simulate``.

fit:      load -> QC -> climatology -> transforms -> marginals -> latent -> dependence,
          then serialize a FittedModel to models/.
simulate: load the FittedModel and generate; cheap. Validates against observed.
"""
from __future__ import annotations

import argparse

import pandas as pd
import xarray as xr
from scipy.special import ndtri  # standard-normal quantile (inverse CDF)

from . import climatology, dependence, io, marginals, transforms, trend
from ._cube import to_matrix
from .config import Config, load_config
from .model import FittedModel
from .simulate import simulate


def fit_model(config: Config) -> FittedModel:
    """Run the full fitting chain and return a serializable FittedModel."""
    rng = config.rng()
    ingest = io.build_dataset(config, rng)
    cube, station_meta = ingest.cube, ingest.station_meta

    # Phase 3: transform raw -> ~Gaussian latent (before climatology; see D3.1)
    tset = transforms.fit(cube, config.variables)
    g = tset.forward(cube)

    # Phase 2: climatology on the transformed variable -> standardized anomaly
    cc = config.section("climatology")
    spec = climatology.HarmonicSpec(
        seasonal=cc["seasonal_harmonics"], diurnal=cc["diurnal_harmonics"],
        interact=cc["interact_seasonal_diurnal"], use_lst=cc["use_local_solar_time"],
    )
    clim = climatology.fit(g, spec)
    anom = clim.standardize(g)

    mat, keys = to_matrix(anom)
    # dry mask (on RAW values) for intermittent variables -> censored marginal
    dry_da = xr.zeros_like(cube, dtype=bool)
    for v in map(str, cube["variable"].values):
        vc = config.variables[v]
        if vc.get("kind") == "intermittent":
            dry_da.loc[{"variable": v}] = cube.sel(variable=v) <= vc.get("wet_threshold_mm", 0.1)
    dry_mat, _ = to_matrix(dry_da)
    q = float(config.section("marginals")["threshold_quantile"])
    margs = marginals.fit(mat, keys, config.variables, q, dry_mat)
    u = margs.to_uniform(mat, dry_mat)
    gauss = ndtri(u)                      # uniforms -> standard normal latent field

    dcfg = config.section("dependence")
    months = pd.DatetimeIndex(cube["time"].values).month.to_numpy()   # season for the innovation cov (D5.3)
    dep = dependence.fit(gauss, months, dcfg["eof_variance"], int(dcfg["var_order"]),
                         dcfg.get("copula", "gaussian"), rng)
    trd = trend.fit(config.section("trend"), config.variables)

    model = FittedModel(
        config_raw=config.raw, station_meta=station_meta,
        var_names=[str(v) for v in cube["variable"].values],
        station_ids=[str(s) for s in cube["station"].values],
        climatology=clim, transforms=tset, marginals=margs, dependence=dep, trend=trd,
        meta={"qc_notes": ingest.report.notes, "n_modes": dep.n_modes},
    )
    return model


def cmd_fit(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    model = fit_model(config)
    out = config.models_dir / "fitted.json"
    model.save(out)
    print(f"[fit] model saved -> {out}  (EOF modes: {model.dependence.n_modes})")


def _build_trend(config: Config, args: argparse.Namespace):
    """Build the climate-trend object at SIMULATE time — SSP scenario and horizon (target year)
    are simulation *inputs* (override config), so one fitted model serves any scenario."""
    from . import trend
    from .cmip6_cds import DEFAULT_MODEL
    tcfg = dict(config.section("trend"))
    if getattr(args, "ssp", None):
        tcfg["ssp"] = args.ssp
    if getattr(args, "target_year", None):
        tcfg["target_year"] = int(args.target_year)
    if getattr(args, "trend", None) is not None:
        tcfg["enabled"] = bool(args.trend)
    if tcfg.get("enabled") and not tcfg.get("cmip6_deltas_path"):
        p = config.models_dir / f"cmip6_deltas_{tcfg['ssp']}_{tcfg['target_year']}_{DEFAULT_MODEL}.npz"
        if p.exists():
            tcfg["cmip6_deltas_path"] = str(p)
        else:
            print(f"[trend] enabled but deltas not found: {p.name}. Run 'fetch-cmip6-deltas' first.")
    return trend.fit(tcfg, config.variables)


def cmd_fetch_cmip6(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    from .cmip6_cds import DEFAULT_MODEL, SSP_MAP, compute_deltas, future_window
    ssp = args.ssp or config.section("trend")["ssp"]
    target = int(args.target_year or config.section("trend")["target_year"])
    model = args.model or DEFAULT_MODEL
    print(f"[cmip6] {ssp} ({SSP_MAP.get(ssp, ssp)}) target {target} "
          f"-> future window {future_window(target)}, model {model}")
    path = compute_deltas(config, ssp, target, model)
    print(f"[cmip6] quantile deltas saved -> {path}")


def cmd_simulate(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    model = FittedModel.load(config.models_dir / "fitted.json")
    model.trend = _build_trend(config, args)          # scenario/horizon are simulate-time inputs
    rng = config.rng()
    sim = simulate(model, config, rng)

    out_dir = config.models_dir.parent / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    nc = out_dir / "simulation.nc"
    ds = xr.Dataset({"obs": sim})
    # provenance: full config + seed embedded for reproducibility
    import json as _json
    ds.attrs.update({
        "weathergen_version": __import__("weathergen").__version__,
        "seed": config.seed, "config_yaml": _json.dumps(config.raw, default=str),
        "trend_enabled": int(model.trend.enabled), "ssp": str(model.trend.ssp),
        "target_year": int(model.trend.target_year),
    })
    ds.to_netcdf(nc)
    print(f"[simulate] {sim.sizes['time']}h x {sim.sizes['station']}st x {sim.sizes['variable']}var "
          f"(trend={'on' if model.trend.enabled else 'off'}) -> {nc}")

    # validate against the observed station record (truth, not ERA5-extended)
    ingest = io.build_dataset(config, config.rng())
    from .validate import validate
    report = validate(ingest.station_cube, sim, config)
    print(f"[validate] report -> {report}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="weathergen", description=__doc__)
    p.add_argument("-c", "--config", default="config.yaml")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("fit", help="fit the generator and serialize it").set_defaults(func=cmd_fit)

    sp_sim = sub.add_parser("simulate", help="simulate from a fitted generator")
    sp_sim.add_argument("--ssp", default=None, help="SSP scenario input, e.g. ssp245 | ssp585 (overrides config)")
    sp_sim.add_argument("--target-year", default=None, help="climate horizon year input (default 2050)")
    sp_sim.add_argument("--trend", dest="trend", action="store_true", default=None, help="enable the climate trend")
    sp_sim.add_argument("--no-trend", dest="trend", action="store_false", help="disable the climate trend")
    sp_sim.set_defaults(func=cmd_simulate)

    sp_c = sub.add_parser("fetch-cmip6-deltas", help="download CMIP6 + compute quantile deltas for an SSP/horizon")
    sp_c.add_argument("--ssp", default=None, help="ssp126 | ssp245 | ssp370 | ssp585")
    sp_c.add_argument("--target-year", default=None, help="horizon year (default 2050)")
    sp_c.add_argument("--model", default=None, help="CMIP6 model (default ec_earth3)")
    sp_c.set_defaults(func=cmd_fetch_cmip6)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
