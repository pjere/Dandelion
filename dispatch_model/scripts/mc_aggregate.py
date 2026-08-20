"""Collapse N Monte-Carlo draws into one workbook of mean / median / sigma / confidence intervals.

Reads `<mc>/draw_*/`'s CSV tables (written alongside each draw's workbooks) and, for every metric, reports
the distribution ACROSS draws at each (year, zone) or (year, zone, tech).

Two choices worth stating, because they change what the numbers mean:

  * **Percentiles are of the draw distribution, not of hours.** p05/p95 of `mean_spot` is "the annual mean
    was this low/high in 5 % of weather years", not "5 % of hours were this cheap". The hourly
    distribution lives in each draw's own price file.
  * **Sigma is the sample standard deviation across draws** (ddof=1), so it is an estimate of weather
    variability, and its own precision goes as 1/sqrt(2(N-1)) — with N=50 the sigma is itself only good to
    about ±10 %. Reported N per row so a thin cell is visible rather than implied.

    python -u -X utf8 -W ignore scripts/mc_aggregate.py --mc reports/mc --out reports/mc/ENSEMBLE
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

#: table -> the columns that identify a row (everything else is a metric to aggregate across draws)
_TABLES = {
    "price_summary": ["year", "zone"],
    "negative_zero_hours": ["year", "zone"],
    "revenue_by_tech": ["year", "zone", "tech"],
    "congestion_rent": ["year", "border"],
    "system_adequacy": ["year", "zone"],
    "weather_summary": ["year"],
}
_QUANTILES = [0.05, 0.25, 0.50, 0.75, 0.95]


def _load(mc: Path, name: str) -> pd.DataFrame:
    frames = []
    for d in sorted(mc.glob("draw_*")):
        f = d / f"{name}.csv"
        if not f.exists():
            continue
        df = pd.read_csv(f)
        df["draw"] = int(d.name.split("_")[1])
        frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def aggregate(df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    keys = [k for k in keys if k in df.columns]
    metrics = [c for c in df.columns
               if c not in keys + ["draw"] and pd.api.types.is_numeric_dtype(df[c])]
    if not metrics:
        return pd.DataFrame()
    g = df.groupby(keys, dropna=False)[metrics]
    out = {"n_draws": g.size()}
    stats = {"mean": g.mean(), "median": g.median(), "sigma": g.std(ddof=1),
             **{f"p{int(q*100):02d}": g.quantile(q) for q in _QUANTILES}}
    wide = pd.concat({k: v for k, v in stats.items()}, axis=1)
    wide.columns = [f"{m}__{s}" for s, m in wide.columns]
    wide = wide.reindex(sorted(wide.columns, key=lambda c: (c.split("__")[0], c)), axis=1)
    wide.insert(0, "n_draws", out["n_draws"])
    return wide.reset_index()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mc", default="reports/mc")
    ap.add_argument("--out", default="reports/mc/ENSEMBLE")
    a = ap.parse_args()
    mc, out = Path(a.mc), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    draws = sorted(mc.glob("draw_*"))
    if not draws:
        print(f"no draw_* directories under {mc}", flush=True)
        return 1
    print(f"[agg] {len(draws)} draw(s): {draws[0].name}..{draws[-1].name}", flush=True)

    tables, fr_paths = {}, []
    for name, keys in _TABLES.items():
        raw = _load(mc, name)
        if raw.empty:
            print(f"  {name}: absent, skipped", flush=True)
            continue
        tables[name] = aggregate(raw, keys)
        tables[name].to_csv(out / f"{name}__ensemble.csv", index=False)
        print(f"  {name}: {raw['draw'].nunique()} draws -> {len(tables[name])} rows", flush=True)

    # the headline fan chart: FR annual mean spot per draw, so the spread is visible as raw numbers
    ps = _load(mc, "price_summary")
    if not ps.empty:
        fan = ps[ps["zone"] == "FR"].pivot_table(index="year", columns="draw", values="mean_spot")
        fan.columns = [f"draw_{c:03d}" for c in fan.columns]
        fan["mean"] = fan.mean(axis=1)
        fan["median"] = fan.median(axis=1)
        fan["sigma"] = fan.filter(like="draw_").std(axis=1, ddof=1)
        for q in _QUANTILES:
            fan[f"p{int(q*100):02d}"] = fan.filter(like="draw_").quantile(q, axis=1)
        fan.to_csv(out / "FR_mean_spot_by_draw.csv")
        tables["FR fan chart"] = fan.reset_index()

    notice = pd.DataFrame([
        ("Monte-Carlo ensemble", f"{len(draws)} independent weather draws"),
        ("", ""),
        ("What varies across draws", "A COMPLETE new 20-year weathergen trajectory per draw (42 stations, "
                                     "8 variables), carried coherently into demand (iii), RES (iv), plant "
                                     "availability (v) and the dispatch. Every draw re-runs step v, so "
                                     "thermal derating and reservoir wetness follow that draw's weather."),
        ("What does NOT vary", "Scenario capacities and demand trajectories (the workbook), commodity "
                               "prices, NTC structure, the fitted markup, and the reference-year calendar. "
                               "This ensemble is weather risk ALONE, not scenario risk."),
        ("Percentiles", "Across DRAWS, not across hours. p05 of mean_spot = the annual mean was this low "
                        "in 5% of weather years. The hourly distribution is in each draw's own price file."),
        ("Sigma", "Sample standard deviation across draws (ddof=1). Its own precision is about "
                  "1/sqrt(2(N-1)) — at N=50 the sigma is good to roughly +/-10%."),
        ("Reproducibility", "Draw d is seeded SeedSequence([master_seed, d]) and is identical whether it "
                            "ran first, last or alone."),
    ], columns=["Field", "Value"])

    xl = out / "projection_20y_analysis_ENSEMBLE.xlsx"
    with pd.ExcelWriter(xl, engine="openpyxl") as w:
        notice.to_excel(w, sheet_name="Notice", index=False)
        for name, df in tables.items():
            df.round(3).to_excel(w, sheet_name=name[:31], index=False)
    print(f"[agg] wrote {xl}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
