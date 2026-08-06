"""PROJECTION BACKCAST — the gate projection has never had, and the harness behind every "layer" number.

Every projection-side change (hydro water values, the nuclear default curve, neighbour flex specs,
policy capacity trajectories, the Iberian topology) has to be argued from mechanism unless it can be
scored: the multi-year gate runs the BACKTEST system and structurally cannot see the projection layer.
This closes that hole by projecting a year we can actually score — 2024, from the 2019 reference — and
comparing three arms:

  A  observed                    truth
  B  backtest (saved SMC)        the dispatch with ACTUAL inputs = the best the LP can do
  C  projection from 2019        the full chain: TYNDP scaling, weather-shape transfer, scheme
                                 evolution, water re-levelling, capacity trajectories

|C−A| is the total projection error, |B−A| the dispatch error the multi-year gate already measures, and
the gap between them — the **layer** column — is what the projection machinery itself contributes. That
decomposition is the point: it separates "the dispatch is wrong here" from "the projection is wrong
here", which are different bugs with different fixes, and it is what localised the Iberian topology gap,
the NL behind-the-meter gap and the Spanish must-run floor.

A projection is a DISTRIBUTIONAL forecast, not an hourly one (arm C carries 2019 weather), so everything
is scored on the price distribution — never hour by hour.

Reference result (2026-08, after the Portugal / must-run / ES-ladder work). Pooled |projection err| 7.1,
|dispatch err| 6.2, **|layer| 2.8** — down from 9.6 at the start of that session:

    zone       observed  backtest  projection | proj err  disp err   layer
    BE             70.3      69.8        76.8 |     6.5      -0.5     7.0
    CH             76.0      87.3        89.4 |    13.4      11.3     2.1
    DE_LU          78.5      77.4        80.2 |     1.7      -1.1     2.8
    ES             63.0      60.5        60.1 |    -3.0      -2.5    -0.5
    FR             58.0      45.3        49.8 |    -8.3     -12.7     4.4
    IT_NORTH      107.4      93.3        93.3 |   -14.1     -14.1     0.0
    NL             77.3      78.5        77.9 |     0.6       1.2    -0.7

NB that table PREDATES PT being scored — it is now a modelled zone with its own observed prices, so the
pooled figures below will shift when it enters. Compare like with like across runs.

Arm B is read from the multi-year gate's saved SMC parquet; without it the run still works and simply
omits the decomposition (proj err is still reported against observed).

Run from dispatch_model/:  python -u -X utf8 -W ignore scripts/projection_backcast.py [target_year]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dispatch_model.config import load_config                              # noqa: E402
from dispatch_model.rolling.backtest import _observed_prices               # noqa: E402
from dispatch_model.rolling.projection import _preload, project_year       # noqa: E402

TARGET = int(sys.argv[1]) if len(sys.argv) > 1 else 2024
REF_YEAR = 2019
ZONES = ["FR", "DE_LU", "BE", "CH", "ES", "PT", "NL", "IT_NORTH"]
#: where the multi-year gate leaves arm B. Searched in order; missing → the run degrades to A vs C.
BT_PATHS = (Path("scratchpad/gate_multiyear"), Path("output/gate_multiyear"))

cfg = load_config("config.yaml")
cfg.section("flexibility")["enabled"] = True

t = time.time()
ref = _preload(cfg, REF_YEAR)
ref["markup"] = None                      # SMC level, so arm C is comparable with arm B (also SMC)
print(f"preload {time.time() - t:.0f}s", flush=True)

# Per-window instrumentation. The first ever attempt at this run was launched through a pipe, which
# buffers until exit — 872 CPU-minutes with no visibility into where it went. Print each window's wall
# time, its §51 re-solve count and its FR negatives, so a slow run is diagnosable WHILE it runs.
import dispatch_model.rolling.projection as _proj                          # noqa: E402

_solve0 = _proj.solve_with_triggers
_wn = [0]


def _timed(*a, **kw):
    from dispatch_model import res_schemes as _rs
    inner0, calls = _rs.solve_multizone, [0]

    def _inner(*ia, **ikw):
        calls[0] += 1
        return inner0(*ia, **ikw)

    _rs.solve_multizone = _inner
    t0 = time.time()
    try:
        out = _solve0(*a, **kw)
    finally:
        _rs.solve_multizone = inner0
    _wn[0] += 1
    fr = out["prices"].get("FR")
    print(f"  [w{_wn[0]:02d}] {time.time() - t0:6.0f}s  LP={calls[0]:>3}  "
          f"FR neg {int((fr < 0).sum()) if fr is not None else '-'}", flush=True)
    return out


_proj.solve_with_triggers = _timed

t = time.time()
_stats, proj = project_year(cfg, TARGET, ref, return_prices=True)          # full year
print(f"projected {TARGET} from {REF_YEAR}: {time.time() - t:.0f}s, {len(proj)} h", flush=True)

obs = _observed_prices(cfg, TARGET, ZONES)
bt, bt_src = None, None
for d in BT_PATHS:
    p = d / f"smc_{TARGET}.parquet"
    if p.exists():
        bt, bt_src = pd.read_parquet(p), p
        break
if bt is not None:
    print(f"arm B: loaded {bt_src}", flush=True)
else:
    print(f"arm B: MISSING — run the multi-year gate for {TARGET} to get the dispatch/layer split; "
          f"reporting A vs C only", flush=True)


def stats(s: pd.Series) -> dict:
    s = s.dropna()
    return {"mean": s.mean(), "p5": s.quantile(.05), "p25": s.quantile(.25), "median": s.median(),
            "p75": s.quantile(.75), "p95": s.quantile(.95),
            "neg": int((s < 0).sum()), "b5": int((s < 5).sum()), "sc": int((s > 200).sum())}


rows = []
print(f"\n{'zone':<9}{'arm':<12}" + "".join(f"{k:>8}" for k in
      ("mean", "p5", "p25", "median", "p75", "p95", "neg", "<+5", ">200")))
for z in ZONES:
    o = obs.get(z)
    if o is None or z not in proj.columns:
        continue
    arms = {"A observed": o, "C projection": proj[z]}
    if bt is not None and z in bt.columns:
        arms["B backtest"] = bt[z]
    for name in ("A observed", "B backtest", "C projection"):
        if name not in arms:
            continue
        st = stats(arms[name])
        rows.append({"zone": z, "arm": name, **st})
        print(f"{z:<9}{name:<12}" + "".join(
            f"{st[k]:>8.0f}" for k in ("mean", "p5", "p25", "median", "p75", "p95", "neg", "b5", "sc")))
    print()

df = pd.DataFrame(rows)
piv = df.pivot_table(index="zone", columns="arm", values="mean")
if {"A observed", "C projection"} <= set(piv.columns):
    piv["proj err"] = piv["C projection"] - piv["A observed"]
    if "B backtest" in piv.columns:
        piv["disp err"] = piv["B backtest"] - piv["A observed"]
        piv["layer"] = piv["proj err"] - piv["disp err"]
    print("=== annual mean €/MWh: projection error vs the dispatch error it inherits ===")
    print(piv.round(1).to_string())
    print(f"\nPOOLED |projection err| {piv['proj err'].abs().mean():.1f}"
          + (f"   |dispatch err| {piv['disp err'].abs().mean():.1f}"
             f"   |layer contribution| {piv['layer'].abs().mean():.1f}" if "disp err" in piv else ""))
