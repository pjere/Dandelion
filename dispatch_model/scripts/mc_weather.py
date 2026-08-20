"""Weather draw generation and per-year summary for the Monte-Carlo projection.

One weathergen draw is a complete new 20-year hourly trajectory for all 42 stations. Steps iii (demand),
iv (RES) and v (plant availability) all read the SAME cube, so pointing `POWERSIM_WEATHER_CUBE` at a
per-draw file makes the whole chain coherent on that draw — which is the property that makes an ensemble
mean anything.

The fitted generator is loaded ONCE per process (2.5 s) and simulated per draw (~60 s), which is the
fit-once/simulate-many design weathergen was built for.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]
_WGEN = _ROOT / "weathergen"

_MODEL = {}          # per-process cache: the fitted generator is 1.5 GB on disk, load it once


def _fitted():
    if "m" not in _MODEL:
        import sys
        if str(_WGEN) not in sys.path:
            sys.path.insert(0, str(_WGEN))
        import argparse as _ap

        from weathergen.cli import _build_trend
        from weathergen.config import load_config
        from weathergen.model import FittedModel
        cfg = load_config(str(_WGEN / "config.yaml"))
        model = FittedModel.load(cfg.models_dir / "fitted.json")
        # the climate trend is a simulate-TIME input (ssp / target year), not part of the fit
        model.trend = _build_trend(cfg, _ap.Namespace(ssp=None, target_year=None, trend=None))
        _MODEL["m"], _MODEL["cfg"] = model, cfg
    return _MODEL["m"], _MODEL["cfg"]


def draw_seed(master_seed: int, draw: int) -> int:
    """Independent, order-free seed per draw — the same SeedSequence discipline `rolling/montecarlo.py`
    uses, so draw d is reproducible whether it runs first, last, or alone."""
    return int(np.random.SeedSequence([int(master_seed), int(draw)]).generate_state(1)[0])


def generate_cube(draw: int, out_path: Path, master_seed: int = 20260629) -> Path:
    """Simulate one 20-year weather trajectory to `out_path`. Returns the path."""
    import xarray as xr

    from weathergen.simulate import simulate
    model, cfg = _fitted()
    sim = simulate(model, cfg, np.random.default_rng(draw_seed(master_seed, draw)))
    got = {str(v) for v in sim["variable"].values}
    if "wind_speed_100m_ms" not in got:
        # step iv rejects a cube without it; fail here rather than 10 minutes into the projection
        raise RuntimeError(f"draw {draw}: cube has no wind_speed_100m_ms (got {sorted(got)}) — "
                           f"check weathergen config `simulate.wind100_model` points at an existing file")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ds = xr.Dataset({"obs": sim})
    ds.attrs.update({"mc_draw": int(draw), "mc_master_seed": int(master_seed),
                     "seed": draw_seed(master_seed, draw)})
    ds.to_netcdf(out_path)
    return out_path


#: heating / cooling degree-day bases (°C), the conventional French thresholds
_HDD_BASE, _CDD_BASE = 17.0, 18.0


def weather_summary(cube_path: Path) -> pd.DataFrame:
    """Per-year national summary of one draw — the tab that tells you WHICH weather produced a price path.

    Station-mean hourly series reduced to the aggregates that actually drive this model: temperature (and
    its degree-days, which drive demand), wind at 100 m (the turbine hub height step iv uses), a solar
    proxy from cloud cover, and precipitation (which sets hydro wetness)."""
    import xarray as xr

    from powersim_core.weather_cube import open_cube
    ds = open_cube(str(cube_path))
    try:
        da = ds["obs"]
        time = pd.to_datetime(da["time"].values)
        if time.tz is None:
            time = time.tz_localize("UTC")
        vmap = {str(v): i for i, v in enumerate(da["variable"].values)}
        arr = da.transpose("time", "station", "variable").values
        nat = {v: np.nanmean(arr[:, :, i], axis=1) for v, i in vmap.items()}
    finally:
        ds.close()

    df = pd.DataFrame(nat, index=time)
    daily_t = df["temperature_c"].resample("D").mean()
    out = []
    for y, g in df.groupby(df.index.year):
        dt = daily_t[daily_t.index.year == y]
        rec = {"year": int(y),
               "temp_mean_C": g["temperature_c"].mean(),
               "temp_min_C": g["temperature_c"].min(),
               "temp_max_C": g["temperature_c"].max(),
               "HDD_17": float(np.clip(_HDD_BASE - dt, 0, None).sum()),
               "CDD_18": float(np.clip(dt - _CDD_BASE, 0, None).sum()),
               "cold_days_below_0C": int((dt < 0).sum()),
               "wind100_mean_ms": g.get("wind_speed_100m_ms", g["wind_speed_ms"]).mean(),
               "wind10_mean_ms": g["wind_speed_ms"].mean(),
               "wind_lull_hours_below_3ms": int((g.get("wind_speed_100m_ms",
                                                       g["wind_speed_ms"]) < 3.0).sum()),
               "cloud_mean_pct": g["cloud_cover_pct"].mean() if "cloud_cover_pct" in g else np.nan,
               "solar_proxy_clearness": (1 - g["cloud_cover_pct"] / 100).mean()
                                        if "cloud_cover_pct" in g else np.nan,
               "precip_total_mm": g["precip_1h_mm"].sum() if "precip_1h_mm" in g else np.nan}
        out.append(rec)
    s = pd.DataFrame(out).set_index("year")
    # wetness: annual precipitation relative to the draw's own 20-year mean — the same normalisation the
    # availability model uses for reservoir inflow, so the hydro budget and this tab tell one story
    if s["precip_total_mm"].mean() > 0:
        s["wetness_vs_draw_mean"] = s["precip_total_mm"] / s["precip_total_mm"].mean()
    return s.reset_index()


def main() -> int:
    ap = argparse.ArgumentParser(description="generate one weathergen draw and summarise it")
    ap.add_argument("--draw", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--master-seed", type=int, default=20260629)
    a = ap.parse_args()
    p = generate_cube(a.draw, Path(a.out), a.master_seed)
    print(f"[wgen] draw {a.draw} -> {p}")
    print(weather_summary(p).round(2).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
