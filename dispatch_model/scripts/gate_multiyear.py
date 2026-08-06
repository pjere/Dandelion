"""THE MULTI-YEAR GATE — the standing acceptance test, and the process fix a regression forced.

Every change of the 2025 campaigns was accepted on 2025 evidence alone. The result was a model tuned to
2025 that gave back 2024 (FR 891 negatives against the frozen gate's 335/352) and 2019 (NL mean +32, 222
scarcity hours against 0 observed) — the mirror image of the failure that launched the campaign. From
here a change is only kept if it survives ACROSS years.

Scores four deliberately different regimes — 2019 (normal), 2022 (crisis), 2024 (the frozen-calibration
year), 2025 (record negatives) — on count, boundary mass (<+5), median, mean and scarcity, per year and
pooled. Solved SMC is cached so re-scoring is instant; `--reuse` skips any year already cached.

WHY BOUNDARY MASS AND log_err. Prices form on discrete rungs, so a strict negative COUNT flips on
sub-cent moves; `<+5` is the same signal without the knife edge. And the pooled aggregate must be
SYMMETRIC in over- vs under-printing: averaging raw ratios is not — 0.32 and 4.79 are both errors, but a
mean treats halving as smaller than doubling — so a genuine fix can *raise* the pooled ratio. 2019 going
0.32 → 0.99 did exactly that. `log_err` = mean |log ratio| is the honest scalar; the raw ratio is kept
per row only because it reads naturally. Do not optimise on it.

This is the gate that must be re-run after any change to the dispatch, and it is what proves a
projection-side change did NOT move the backtest — the `layer` decomposition in
`scripts/projection_backcast.py` consumes the SMC parquets this writes.

Reference (2026-08): pooled log_err 1.68, |mean error| 13.8 €/MWh, FR log_err 0.54, FR-2024 negatives
428/352. NB PT is scored from this revision on — it has been a modelled zone since the Iberian work but
the zone list was frozen at seven and omitted it, so pooled figures shift here. Compare like with like.

Run from dispatch_model/:  python -u -X utf8 -W ignore scripts/gate_multiyear.py [--reuse]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dispatch_model.config import load_config                              # noqa: E402
from dispatch_model.rolling import backtest as bt                          # noqa: E402
from dispatch_model.rolling.backtest import _observed_prices               # noqa: E402

cfg = load_config("config.yaml")
#: solved-SMC cache. Deliberately under scratchpad/: the SCRIPT is source, its output is a rebuildable
#: artifact. `scripts/projection_backcast.py` reads arm B from here.
OUT = Path("scratchpad/gate_multiyear")
OUT.mkdir(parents=True, exist_ok=True)
YEARS = (2019, 2022, 2024, 2025)
ZONES = ["FR", "DE_LU", "BE", "CH", "ES", "PT", "NL", "IT_NORTH"]
REUSE = "--reuse" in sys.argv

for y in YEARS:
    dest = OUT / f"smc_{y}.parquet"
    if dest.exists() and REUSE:
        print(f"{y}: reusing {dest}", flush=True)
        continue
    t = time.time()
    # write_lake=False is REQUIRED here: a gate run must never overwrite the golden `backtest_prices`
    # artifact with a calibration-sweep result.
    r = bt.run_backtest(cfg, y, flexibility=True, write_lake=False)
    r["model_prices"].to_parquet(dest)
    print(f"{y}: solved {time.time() - t:.0f}s", flush=True)

rows = []
for y in YEARS:
    m = pd.read_parquet(OUT / f"smc_{y}.parquet")
    o = _observed_prices(cfg, y, ZONES)
    for z in ZONES:
        if z not in m.columns or o.get(z) is None:
            continue
        mm = m[z].dropna()
        oo = o[z].reindex(mm.index).dropna()
        mm = mm.reindex(oo.index)
        if len(mm) < 100:
            continue
        rows.append({"year": y, "zone": z, "m_neg": int((mm < 0).sum()), "o_neg": int((oo < 0).sum()),
                     "m_b5": int((mm < 5).sum()), "o_b5": int((oo < 5).sum()),
                     "m_med": float(mm.median()), "o_med": float(oo.median()),
                     "m_mean": float(mm.mean()), "o_mean": float(oo.mean()),
                     "m_200": int((mm > 200).sum()), "o_200": int((oo > 200).sum())})
df = pd.DataFrame(rows)

print(f"\n{'year':<6}{'zone':<9}{'neg m/o':>13}{'<+5 m/o':>14}{'median m/o':>15}"
      f"{'mean m/o':>15}{'>200 m/o':>12}")
for _, r in df.iterrows():
    print(f"{r.year:<6}{r.zone:<9}{f'{r.m_neg}/{r.o_neg}':>13}{f'{r.m_b5}/{r.o_b5}':>14}"
          f"{f'{r.m_med:.0f}/{r.o_med:.0f}':>15}{f'{r.m_mean:.0f}/{r.o_mean:.0f}':>15}"
          f"{f'{r.m_200}/{r.o_200}':>12}")

df["b5_ratio"] = df["m_b5"] / df["o_b5"].clip(lower=1)
df["log_err"] = np.abs(np.log(df["b5_ratio"].clip(lower=1e-3)))
df["mean_err"] = df["m_mean"] - df["o_mean"]
print("\n=== per-year verdict (ratio 1.0 = perfect; log_err 0 = perfect; mean error €/MWh) ===")
print(df.groupby("year")[["b5_ratio", "log_err", "mean_err"]].mean().round(2).to_string())
print("\n=== per-zone across years ===")
print(df.groupby("zone")[["b5_ratio", "log_err", "mean_err"]].mean().round(2).to_string())
print(f"\nPOOLED  log_err {df['log_err'].mean():.2f} (target 0)   "
      f"|mean error| {df['mean_err'].abs().mean():.1f} €/MWh   "
      f"scarcity recall {df['m_200'].sum()}/{df['o_200'].sum()}"
      f"\n(raw boundary-ratio mean {df['b5_ratio'].mean():.2f} — asymmetric, do not optimise on it)")
