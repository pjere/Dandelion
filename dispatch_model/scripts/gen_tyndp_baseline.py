"""Add the REFERENCE-YEAR (2019) baseline rows to `dispatch_tyndp`.

`tyndp_factors` computes `interp(target) / interp(reference_year)`. The tab's anchors started at 2025,
so `interp(2019)` extrapolated to the 2025 value and every factor was really *target ÷ 2025* — applied
to a 2019 stack and weather shape. The whole 2019→2025 structural change was silently dropped:
measured, every projected year realised `actual2019 × target/2025` of RES instead of `target`
(FR solar 41 %, ES solar 23 %), and a 2024 target got factors of exactly 1.0 — the projection replayed
2019 unchanged (`scratchpad/projection_backcast_2024.py`).

This writes the missing 2019 anchor from ACTUAL data, so the factor becomes target ÷ actual-2019 applied
to the actual-2019 stack. Rules:
  * only for a variable the zone ALREADY anchors in the future — inventing a lone 2019 row would replace
    the deliberate CAGR fallback with a constant 1.0;
  * `cap_flex_gw` is excluded: `flex_capacity_mw` reads it as an ABSOLUTE level, and its own docstring
    records that there is no reference-year flex fleet to scale from;
  * `cap_nuclear_gw` is left to `gen_nuclear_trajectory.py`, which already emits a policy-derived 2019.
Run from dispatch_model/:  python -X utf8 scripts/gen_tyndp_baseline.py [--write]
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dispatch_model.config import load_config                              # noqa: E402
from dispatch_model.io.entsoe_hist import load_demand_hist, load_installed_capacity   # noqa: E402
from dispatch_model.neighbours.blocks import constituents                  # noqa: E402

REF_YEAR = 2019
# `cap_hydro_gw` is EXCLUDED: its anchors are defined inconsistently across zones — FR's 26 GW is total
# hydro incl. pumped storage (France has 25.7 GW), DE's 5 GW is reservoir+ROR only (DE total is ~11 GW).
# No single tech set reproduces both, so any baseline invents a factor: measured, PSP-inclusive gave DE
# 0.34 (deleting two-thirds of German hydro), PSP-exclusive gave FR 1.41 (inventing 41 % growth). The
# anchors are near-flat over the horizon (FR 26→27, DE 5→5), so the pre-existing target/2025 ≈ 1.0 is
# closer to right than either. Fix the sheet's hydro definition first, then baseline it.
SKIP = {"cap_flex_gw", "cap_nuclear_gw", "cap_hydro_gw"}
#: TYNDP variable → the installed-capacity techs that make it up
VAR2TECH = {
    "cap_solar_gw": ("solar",), "cap_wind_gw": ("wind_onshore", "wind_offshore"),
    "cap_gas_gw": ("gas",), "cap_coal_gw": ("coal",), "cap_lignite_gw": ("lignite",),
    "cap_oil_gw": ("oil",), "cap_biomass_gw": ("biomass",),
    # `cap_hydro_gw` counts PSP: no zone defines `cap_psp_gw`, and the anchors are plainly TOTAL hydro
    # (FR 26 GW ≈ France's 25.7 GW of reservoir + ROR + pumped storage). Baselining on reservoir+ROR
    # alone would read 19.2 GW for FR and manufacture 41 % hydro growth by 2040 out of a definitional
    # mismatch. The baseline must be measured the way the anchor is defined.
    "cap_hydro_gw": ("hydro_reservoir", "hydro_ror", "hydro_psp"), "cap_psp_gw": ("hydro_psp",),
}

cfg = load_config("config.yaml")
wb_path = cfg.resolve(cfg.section("assumptions")["workbook"])
sheet = pd.read_excel(wb_path, sheet_name="dispatch_tyndp")


def actual(zone: str, variable: str) -> float | None:
    zs = constituents(zone)
    if variable == "demand_twh":
        try:
            d = load_demand_hist(cfg, REF_YEAR, zones=zs)
        except Exception:                                                  # noqa: BLE001
            return None
        return round(float(d["load_mw"].sum()) / 1e6, 1) if not d.empty else None
    techs = VAR2TECH.get(variable)
    if not techs:
        return None
    tot = 0.0
    for z in zs:
        try:
            cap = load_installed_capacity(cfg, z, REF_YEAR) or {}
        except Exception:                                                  # noqa: BLE001
            continue
        tot += sum(float(cap.get(t, 0.0)) for t in techs)
    return round(tot / 1e3, 3) if tot > 0 else None


rows, skipped = [], []
for (zone, variable), g in sheet.groupby(["zone", "variable"]):
    if variable in SKIP:
        continue
    if not (g["year"] > REF_YEAR).any():                                   # no future anchor → leave alone
        continue
    v = actual(str(zone), str(variable))
    if v is None:
        skipped.append(f"{zone}/{variable}")
        continue
    future = g[g["year"] > REF_YEAR].sort_values("year")
    rows.append({"zone": zone, "variable": variable, "year": REF_YEAR, "value": v,
                 "first_anchor": f"{int(future.iloc[0].year)}={future.iloc[0].value:g}"})

df = pd.DataFrame(rows)
print(f"{REF_YEAR} baselines derived from actuals ({len(df)} rows):")
for z, g in df.groupby("zone"):
    bits = [f"{r.variable.replace('cap_', '').replace('_gw', ''):<8}{r.value:>8.1f}  (vs {r.first_anchor})"
            for r in g.itertuples()]
    print(f"  {z}:")
    for b in bits:
        print(f"      {b}")
if skipped:
    print(f"\nno actual available (left to the existing behaviour): {skipped}")

if "--write" not in sys.argv:
    print("\n(dry run — pass --write to update the workbook)")
    raise SystemExit

from openpyxl import load_workbook                                         # noqa: E402

wb = load_workbook(wb_path)
ws = wb["dispatch_tyndp"]
hdr = {c.value: i + 1 for i, c in enumerate(ws[1])}
zc, vc, yc, valc = hdr["zone"], hdr["variable"], hdr["year"], hdr["value"]
existing = {(ws.cell(r, zc).value, ws.cell(r, vc).value, int(ws.cell(r, yc).value)): r
            for r in range(2, ws.max_row + 1) if ws.cell(r, yc).value is not None}
repl = add = 0
for r_ in rows:
    key = (r_["zone"], r_["variable"], REF_YEAR)
    if key in existing:
        ws.cell(existing[key], valc).value = r_["value"]
        repl += 1
    else:
        rr = ws.max_row + 1
        ws.cell(rr, zc).value = r_["zone"]
        ws.cell(rr, vc).value = r_["variable"]
        ws.cell(rr, yc).value = REF_YEAR
        ws.cell(rr, valc).value = r_["value"]
        add += 1
wb.save(wb_path)
print(f"\nworkbook updated: {repl} replaced, {add} added")
