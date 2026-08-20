"""Run the projection horizon and persist PRICES, VOLUMES and FLOWS per year.

`project_trajectory` returns a price summary, which is all the gate ever needed. Revenues and congestion
rents need the volume each price was paid on, so this runner drives `project_year` directly with a `sink`
and `DISPATCH_CAPTURE_DISPATCH=1`, writing one parquet set per year:

    <out>/prices_<year>.parquet     hourly SMC and post-markup spot, per zone
    <out>/dispatch_<year>.parquet   hourly MW per (zone, tech), incl. res / ens / dump / storage
    <out>/flows_<year>.parquet      hourly net MW per directed border

The preload is paid once for the whole horizon (~4 min); each year is then ~1 min. Re-running skips years
whose parquet already exists, so an interrupted horizon resumes.

    python -u -X utf8 -W ignore scripts/run_projection_20y.py [--start 2027] [--end 2046] [--draw 0]

This writes nothing to the lake and nothing to reports/ — only to the output directory given.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd                                                            # noqa: E402

from dispatch_model.config import load_config                                  # noqa: E402
from dispatch_model.rolling.projection import _preload, project_year           # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--start", type=int, default=None)
    ap.add_argument("--end", type=int, default=None)
    ap.add_argument("--ref-year", type=int, default=2019)
    ap.add_argument("--draw", type=int, default=0)
    ap.add_argument("--out", default="scratchpad/proj20y")
    ap.add_argument("--n-weeks", type=int, default=None, help="truncate each year (smoke runs)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    hz = cfg.section("projection").get("horizon", {})
    start = args.start or int(hz.get("start_year", 2027))
    end = args.end or int(hz.get("end_year", 2046))
    years = list(range(start, end + 1))

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # the capture is what makes this run different from the gate's; set before any solve
    os.environ["DISPATCH_CAPTURE_DISPATCH"] = "1"

    todo = [y for y in years if not (out / f"prices_{y}.parquet").exists()]
    print(f"[20y] horizon {start}-{end} ({len(years)} years), {len(todo)} to run, draw={args.draw}",
          flush=True)
    if not todo:
        print("[20y] nothing to do", flush=True)
        return 0

    t0 = time.monotonic()
    ref = _preload(cfg, args.ref_year)
    print(f"[20y] preload {time.monotonic() - t0:.0f}s  zones={len(ref['zones'])}  "
          f"markup={'yes' if ref.get('markup') else 'NO'}", flush=True)

    provider = None
    if cfg.section("projection").get("weather_coherent", True):
        from dispatch_model.weather_shapes import default_weather_provider
        provider = default_weather_provider

    stats = []
    for y in todo:
        t1 = time.monotonic()
        ws = None
        if provider is not None:
            try:
                ws = provider(cfg, y, draw=args.draw, ref_year=args.ref_year)
            except Exception as exc:                                           # noqa: BLE001
                print(f"[20y] {y}: weather-coherent engines unavailable ({type(exc).__name__}: {exc}); "
                      f"falling back to reshaped reference-year weather", flush=True)
                provider = None
        sink: dict = {}
        st = project_year(cfg, y, ref, n_weeks=args.n_weeks, weather_shapes=ws, draw=args.draw, sink=sink)
        stats.append(st)

        smc, spot = sink["smc"], sink["spot"]
        px = pd.concat({"smc": smc, "spot": spot}, axis=1)
        px.index.name = "timestamp_utc"
        px.to_parquet(out / f"prices_{y}.parquet")
        if sink.get("dispatch") is not None:
            d = sink["dispatch"]
            d.columns = [f"{z}|{t}" for z, t in d.columns]                     # parquet needs flat columns
            d.index.name = "timestamp_utc"
            d.to_parquet(out / f"dispatch_{y}.parquet")
        if sink.get("flows") is not None:
            sink["flows"].to_parquet(out / f"flows_{y}.parquet", index=False)

        drop = sink.get("dropped") or []
        print(f"[20y] {y} done in {time.monotonic() - t1:.0f}s  hours={len(spot)}  "
              f"FR mean {spot['FR'].mean():.1f} (smc {smc['FR'].mean():.1f})"
              + (f"  DROPPED {len(drop)} window(s): {drop}" if drop else ""), flush=True)

    pd.concat(stats, ignore_index=True).to_csv(out / "stats.csv", index=False)
    print(f"[20y] complete in {(time.monotonic() - t0) / 60:.1f} min -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
