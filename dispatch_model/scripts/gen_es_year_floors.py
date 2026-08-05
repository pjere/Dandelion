"""Year-index the ES bid ladder in `dispatch_res_schemes`, with floors measured per year.

The tab held ONE Spanish block, calibrated on 2025 (its own source note says so, and `merchant` -1
reproduces the 2025 median of negative hours exactly). Spain's negative-price regime is only two years
old and changed sharply between them, so a static ladder cannot be right for both:

    year   n<0   %hrs    p50     p10      min   in (-1,0)   hours in [0,10)
    2022     0      -      -       -        -           -                 -
    2023     0      -      -       -        -           -                 -
    2024   247    2.8  -0.10   -1.01    -2.00        89 %             1 657
    2025   556    6.3  -1.00   -5.80   -15.00        52 %             1 170

Run against 2024, the 2025 ladder puts 100 % of Spanish RES below zero with 30 % at -10 — five times
deeper than anything Spain printed that year — so every surplus hour clears negative: 1329 modelled
negative hours against 247 observed. The 1082 excess matches the 1657 observed hours in [0, 10) that a
too-deep ladder drags under.

FLOORS ARE MEASURED, using the convention already in the tab: `merchant` takes the MEDIAN of the year's
negative prints, the deep subsidised rung takes their p10. For 2022-23 Spain printed no negative hours
at all, so both floors are 0.0 — a static ladder over-prints those years too. Shares are left untouched;
only depth is at issue (Spanish RES does not curtail into negatives — its capacity factor RISES from
0.203 above EUR 40 to 0.350 in negative hours, so it genuinely bids below zero, just barely).

Run from dispatch_model/:  python -X utf8 scripts/gen_es_year_floors.py [--write]
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dispatch_model.config import load_config                              # noqa: E402
from dispatch_model.rolling.backtest import _observed_prices               # noqa: E402

ZONE, YEARS = "ES", (2022, 2023, 2024, 2025)
TRIGGER = {"recore": 0, "merchant": 0}

# THE SHARE IS THE LEVER FOR THE COUNT; THE FLOOR ONLY SETS THE DEPTH. A negative hour occurs whenever
# residual demand falls below the volume bidding under zero — how far under zero that volume bids decides
# only how deep the print goes. Measured directly: re-running 2024 with the floors alone corrected from
# -10/-1 to -1.01/-0.10 moved the modelled negative count by 19 hours out of ~1900 excess (2109 -> 2128).
#
# So the below-zero VOLUME is calibrated to the observed frequency of negatives: the price is negative
# iff residual demand < that volume, hence volume = the net-load quantile at the observed negative share.
#
#     year   negatives   net-load quantile   => below-zero share of must-take RES
#     2024       2.81 %          2 209 MW                        16.9 %
#     2025       6.23 %          4 031 MW                        30.3 %
#
# `recore` is the subsidised tranche that will pay to generate to keep its regulated payment, so it
# carries the below-zero volume; `merchant` bids 0.0 — a merchant plant does not pay to generate. The
# tab had BOTH below zero, i.e. 100 % of Spanish RES bidding negative, so every surplus hour cleared
# negative by construction. Note 2025's measured 30.3 % lands on the tab's existing 30 % recore share:
# the share was right for the year it was calibrated on, and it is `merchant` that was misplaced.
MERCHANT_FLOOR = 0.0
BELOW_ZERO_SHARE = {2022: 0.0, 2023: 0.0, 2024: 0.169, 2025: 0.303}

cfg = load_config("config.yaml")
wb_path = cfg.resolve(cfg.section("assumptions")["workbook"])

rows = []
print(f"{'year':<6}{'n<0':>6}{'%hrs':>7}{'p50':>8}{'p10':>8}   recore share   recore floor   merchant floor")
for y in YEARS:
    s = _observed_prices(cfg, y, [ZONE]).get(ZONE)
    if s is None or s.dropna().empty:
        continue
    s = s.dropna(); neg = s[s < 0]
    share = BELOW_ZERO_SHARE[y]
    if neg.empty:                                     # no negative regime -> nothing bids below zero
        recore_floor, src = 0.0, f"measured {y}: no negative hours observed -> nothing bids below zero"
        print(f"{y:<6}{0:>6}{0.0:>7.2f}{'-':>8}{'-':>8}{share:>15.3f}{recore_floor:>15.2f}"
              f"{MERCHANT_FLOOR:>17.2f}")
    else:
        recore_floor = round(float(neg.quantile(0.10)), 2)     # depth of the subsidised rung
        src = (f"measured {y}: {len(neg)} neg h ({100*len(neg)/len(s):.2f}% of hours), p50 "
               f"{neg.median():.2f}, p10 {neg.quantile(.1):.2f}, min {neg.min():.2f}; below-zero share "
               f"= net-load quantile at the observed negative frequency")
        print(f"{y:<6}{len(neg):>6}{100*len(neg)/len(s):>7.2f}{neg.median():>8.2f}{neg.quantile(.1):>8.2f}"
              f"{share:>15.3f}{recore_floor:>15.2f}{MERCHANT_FLOOR:>17.2f}")
    for sch, sh, floor in (("recore", share, recore_floor),
                           ("merchant", 1.0 - share, MERCHANT_FLOOR)):
        rows.append({"zone": ZONE, "scheme": sch, "volume_share": round(sh, 3),
                     "bid_floor_eur_mwh": floor, "trigger_hours": TRIGGER[sch], "year": y,
                     "source": src, "scenario": "reference"})

print(f"\n{len(rows)} dated ES rows. Years beyond {max(YEARS)} inherit {max(YEARS)} (latest vintage <= year).")
if "--write" not in sys.argv:
    print("(dry run — pass --write to update the workbook)")
    raise SystemExit

from openpyxl import load_workbook                                         # noqa: E402

wb = load_workbook(wb_path)
ws = wb["dispatch_res_schemes"]
hdr = {c.value: i + 1 for i, c in enumerate(ws[1]) if c.value}
if "year" not in hdr:                                  # add the column once, at the end
    col = ws.max_column + 1
    ws.cell(1, col).value = "year"
    hdr["year"] = col
    print(f"added 'year' column at index {col}")
ycol = hdr["year"]

# Drop EVERY existing row for this zone — dated and undated alike — before writing. Removing only the
# undated ones makes the script non-idempotent: a second run leaves the previous dated block in place,
# the loader then returns both, and `volume_share` normalises across the union (observed: shares of
# 0.15/0.35/0.084/0.416 instead of 0.169/0.831).
drop = [r for r in range(ws.max_row, 1, -1) if ws.cell(r, hdr["zone"]).value == ZONE]
for r in drop:
    ws.delete_rows(r)
print(f"removed {len(drop)} existing {ZONE} row(s) (idempotent rewrite)")

for d in rows:
    rr = ws.max_row + 1
    for k, v in d.items():
        if k in hdr:
            ws.cell(rr, hdr[k]).value = v
wb.save(wb_path)
print(f"workbook updated: {len(rows)} dated {ZONE} rows written")
