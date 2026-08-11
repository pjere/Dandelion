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

⚠ ARM B GOES STALE. It is read from the multi-year gate's CACHE, so any change to the DISPATCH — a new
zone, a must-run floor, an NTC — invalidates it until the gate is re-run. Comparing a fresh projection
against a stale backtest silently corrupts the whole decomposition, and it has already happened: after
Portugal and the ES must-run floor landed in the backtest, several backcasts were read off pre-Portugal
parquets, and Spain's layer was reported as -0.5 when it was really -6.8 (its arm B moved 60.5 → 66.9
once the changes were actually in it). RE-RUN `scripts/gate_multiyear.py` BEFORE trusting a layer number
after any dispatch change.

Reference result (2026-08, after GB's promotion to a modelled zone and the performance campaign, with GB
now scored). Pooled |projection err| 7.9, |dispatch err| 9.5, |layer| 6.8 — NOT comparable with the
previous pooled row, which was taken over eight zones rather than nine:

    zone       observed  backtest  projection | proj err  disp err   layer
    BE             70.3      68.2        77.8 |     7.4      -2.1     9.6
    CH             76.0      90.1        90.8 |    14.8      14.1     0.7
    DE_LU          78.5      75.7        78.5 |    -0.0      -2.8     2.8
    ES             63.0      68.2        64.2 |     1.1       5.1    -4.0
    FR             58.0      63.1        61.4 |     3.4       5.1    -1.7
    GB             84.5      70.9        60.2 |   -24.3     -13.6   -10.7
    IT_NORTH      107.4      93.6        91.4 |   -16.0     -13.8    -2.2
    NL             77.3     100.8        75.1 |    -2.2      23.5   -25.7
    PT             63.5      69.0        65.3 |     1.8       5.6    -3.7

Previous reference, eight zones: pooled |proj err| 6.3, |disp err| 6.0, |layer| 3.5, with BE +8.4 and
ES -6.8 the open items.

FR IMPROVED MATERIALLY (proj err -8.3 -> +3.4, its backtest arm moving 47.5 -> 63.1): promoting GB removed
the 4000 MW of phantom "GB import" tranches that had sat inside the FR stack, and this harness sees it as
clearly as the multi-year gate does. ES also improved (layer -6.8 -> -4.0).

GB IS NOW THE WORST ZONE, and both halves of its error have identified causes.
  * the -13.6 DISPATCH error is `io.gb_embedded` netting ~5 GW of reconstructed embedded generation off
    demand, which leaves GB structurally long and cheap. The gate scores it at 1800 hours below +5 EUR/MWh
    in 2019 against 9 observed. That trade was taken deliberately (see `DECISIONS.md`) because the
    alternative — pricing the block — re-opened a VoLL cascade into NL;
  * the further -10.7 of LAYER is almost certainly the TYNDP gap. The coverage report prints
    `GB: cap_solar_gw=missing, cap_wind_gw=missing, demand_twh=missing`, so GB falls back to the generic
    CAGR: RES +4.5 %/yr against demand +0.8 %/yr, i.e. RES x1.25 vs demand x1.04 over 2019-2024. That
    over-grows surplus in exactly the direction observed. It is the same defect class `TYNDP_SOURCES.md`
    records as having cost NL 458 negative hours; GB acquired it when it was promoted without trajectories,
    and filling those rows is the actionable item.

NL's -25.7 LAYER IS AN ARTEFACT — do not read it as a projection defect. Its dispatch arm is +23.5
(backtest 100.8 against 77.3 observed) while its projection arm is -2.2: the projection lands near-correct
against a badly over-priced backtest. The two arms do not run the same NL topology — the backtest uses
published hourly NTC where it exists (`assemble.hourly_ntc`), the projection the 2019 reference-year
flow-derived scalars. A layer that large is measuring that difference between arms, not a layer effect,
and it stays uninterpretable until the two agree on NL's borders.

Without arm B the run still works and simply omits the decomposition, reporting proj err against
observed only.

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
#: GB joins the scored set now that it is a MODELLED zone rather than a border curve. It was excluded when
#: it had no balance of its own, and that exclusion outlived its reason: GB now feeds FR, BE, NL and DK
#: through four borders, so leaving it unscored means the decomposition cannot see whether GB is distorting
#: its neighbours' layers — and the gate's own census says GB is systematically cheap (2019: 1800 hours
#: below +5 EUR/MWh against 9 observed). BE, whose layer is the largest open item, sits on a GB border.
#:
#: Adding a zone MOVES THE POOLED FIGURES, so the reference table below is not comparable across this
#: change on its pooled row; the per-zone rows are.
ZONES = ["FR", "DE_LU", "BE", "GB", "CH", "ES", "PT", "NL", "IT_NORTH"]
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
