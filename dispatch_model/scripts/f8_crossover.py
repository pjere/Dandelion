"""The CROSS-OVER: negative-hour COUNT rises with RES build-out while negative-price DEPTH attenuates as
legacy support expires. Two effects moving in opposite directions — the structural claim the static
`dispatch_res_schemes` tab could never represent, and the acceptance test for the whole projection story.

Samples 2028/2034/2040/2046 over Jan–Jun windows, FLEX on, at the SMC level (the step-vii markup is
disabled: the wedge would blur the structural signal). Per year it prints the FR negative count, the
depth split, the deepest print, and the OA/CR/merchant shares plus the §51 trigger that drive the
attenuation.

Promoted out of `scratchpad/` because it is the projection's acceptance test, not a throwaway probe —
and because two defects had accumulated there unversioned: the year list had been narrowed to
`(2034,)` for a one-off seam bisection with the 2028 row pasted in as literal text, and the share
columns read 0 for every vintaged scheme (see `_family`).

Reference result (2026-08, after the Iberian/must-run/ladder work):

    year  trig  OA%  CR%  mer% | neg_h  mean_neg
    2028    1h   34   58     8 |  2418     -0.70
    2034    1h   10   58    32 |  3577     -0.61
    2040    1h    4   42    54 |  3875     -0.44
    2046    1h    0    0   100 |  3727     -0.01

Count rises, depth attenuates to nothing as the paid-regardless stock expires. The LEVELS follow from
the workbook holding 64 GW of French nuclear until 2035 against RES ×5.5 by 2034 and demand ×1.1 — the
LP is reporting that trajectory's implication, so read the levels as a statement about the scenario.

Run from dispatch_model/:  python -u -X utf8 -W ignore scripts/f8_crossover.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dispatch_model.config import load_config                              # noqa: E402
from dispatch_model.rolling.projection import _preload, project_year       # noqa: E402
from dispatch_model.scheme_evolution import scheme_shares, trigger_hours   # noqa: E402

YEARS = (2028, 2034, 2040, 2046)
N_WEEKS = 26

cfg = load_config("config.yaml")
cfg.section("flexibility")["enabled"] = True
t = time.time()
ref = _preload(cfg, 2019)
ref["markup"] = None                                   # structural read: SMC, no wedge
print(f"preload {time.time() - t:.0f}s   flex on, markup off", flush=True)

# Per-window instrumentation: wrap the solver entry point inside the projection module so each weekly
# window prints its wall time and FR negative count — localises whether a slow year is uniform (§51
# iterations) or a few pathological windows.
import dispatch_model.rolling.projection as proj                           # noqa: E402

_solve0 = proj.solve_with_triggers
_wn = [0]


def _timed(*a, **kw):
    t0 = time.time()
    _wn[0] += 1
    # A SEAM attempt failing is NOT a lost window. `project_year` tries the seam-linked spec and falls
    # back to the cold one, and the cold attempt solves — measured on the 20-year run: ~24 seam attempts
    # fail, ZERO windows are dropped, every year returns its full 4368 h, and two runs with different
    # failure sets agree to within 2 h in 3577. The cause is a real conflict between the inherited
    # reactor state and the new window's C6/C7 fleet rows (see `lp.highs_solver`, F5 seam C3-ramp), and
    # the fallback is the designed handling of it. Labelled explicitly here because the bare "FAILED"
    # this used to print was read as a lost window three times over.
    seam = bool(kw.get("flex") and any("u_init" in s for s in kw["flex"].values()))
    try:
        out = _solve0(*a, **kw)
    except Exception as e:                             # noqa: BLE001 — report, then re-raise to the caller
        tag = "seam attempt failed → cold retry follows" if seam else "COLD attempt failed → window lost"
        print(f"    [w{_wn[0]:02d}] {time.time() - t0:5.0f}s  {tag}: {str(e)[:80]}", flush=True)
        raise
    fr = out["prices"].get("FR")
    print(f"    [w{_wn[0]:02d}] {time.time() - t0:5.0f}s  FR neg "
          f"{int((fr < 0).sum()) if fr is not None else '-'}", flush=True)
    return out


proj.solve_with_triggers = _timed


def _family(d: dict) -> dict:
    """Aggregate vintaged scheme names onto their family.

    `scheme_evolution` emits vintaged keys (`complement_remuneration@6h`), so a bare
    `sh.get("complement_remuneration")` reads 0. Measured before this fix: the columns printed
    OA 5 / CR 0 / mer 32 — summing to 37 % — when `scheme_shares` actually returns 1.001.
    """
    out: dict = {}
    for k, v in d.items():
        out[k.split("@")[0]] = out.get(k.split("@")[0], 0.0) + v
    return out


print(f"\n{'year':>5} {'trig':>4} {'OA%':>5} {'CR%':>5} {'mer%':>5} | {'neg_h':>5} {'sh(-5,0)':>8} "
      f"{'mid(-50,-5)':>11} {'deep<-50':>8} {'min':>7} {'mean_neg':>8}", flush=True)
for year in YEARS:
    t = time.time()
    sh = _family({s["scheme"]: s["share"] for s in
                  scheme_shares("FR", year, {}, reg=ref.get("res_registry", {}).get("FR"))})
    _stats, spot = project_year(cfg, year, ref, n_weeks=N_WEEKS, return_prices=True)
    p = spot["FR"].dropna()
    neg = p[p < 0]
    print(f"{year:>5} {trigger_hours(year):>3}h {100 * sh.get('obligation_achat', 0):>4.0f} "
          f"{100 * sh.get('complement_remuneration', 0):>4.0f} {100 * sh.get('merchant', 0):>4.0f} | "
          f"{len(neg):>5} {int(((p >= -5) & (p < 0)).sum()):>8} {int(((p >= -50) & (p < -5)).sum()):>11} "
          f"{int((p < -50).sum()):>8} {p.min():>7.1f} {neg.mean() if len(neg) else 0:>8.2f}   "
          f"({time.time() - t:.0f}s, {len(p)}h)", flush=True)
print("\ncross-over expectation: neg_h RISES with RES build-out; depth (mid/deep mass, |mean_neg|) "
      "ATTENUATES as OA rolls off and the trigger tightens to 1h.")
