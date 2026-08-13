"""Audit `io.unclassified_gen`: what each zone's dropped generation is, and how it is now split.

Prints, per modelled zone, the four unclassified techs' energy and the must-run / RES / still-dropped
decomposition, plus the day/night ratio that decides whether `other`'s variable part is credited as solar.

Run from dispatch_model/:  python -X utf8 -W ignore scripts/audit_unclassified.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dispatch_model.config import load_config                                        # noqa: E402
from dispatch_model.io.entsoe_hist import load_generation_hist                       # noqa: E402
from dispatch_model.io.unclassified_gen import (                                     # noqa: E402
    _FLAT_MUSTRUN, _SOLAR_DAY_NIGHT, _split_other, components,
)
from dispatch_model.neighbours.blocks import ZONE_AGGREGATES, constituents           # noqa: E402

YEAR = int(sys.argv[1]) if len(sys.argv) > 1 else 2024
ZONES = ["FR", "DE_LU", "BE", "NL", "CH", "ES", "PT", "IT_NORTH", "GB", *ZONE_AGGREGATES]


def main() -> None:
    cfg = load_config("config.yaml")
    print(f"unclassified generation, {YEAR} — TWh unless stated\n")
    print(f"{'zone':<10}{'flat':>8}{'other':>8}{'d/n':>6}{'  ':<2}"
          f"{'-> mustrun':>11}{'-> RES':>9}{'dropped':>9}   note")
    tot = {"flat": 0.0, "other": 0.0, "mustrun": 0.0, "res": 0.0, "drop": 0.0}
    for z in ZONES:
        zs = constituents(z)
        g = load_generation_hist(cfg, YEAR, zones=zs)
        if g.empty:
            continue
        flat = g[g["tech"].isin(_FLAT_MUSTRUN)].groupby("timestamp_utc")["gen_mw"].sum()
        oth = g[g["tech"] == "other"].groupby("timestamp_utc")["gen_mw"].sum()
        e_flat = float(flat.sum()) / 1e6 if len(flat) else 0.0
        e_oth = float(oth.sum()) / 1e6 if len(oth) else 0.0
        ratio = _split_other(oth)[2] if len(oth) else 0.0
        comp = components(cfg, zs, YEAR)
        mr = float(comp["mustrun_mw"].sum()) / 1e6 if comp is not None else 0.0
        rs = float(comp["res_mw"].sum()) / 1e6 if comp is not None else 0.0
        drop = e_flat + e_oth - mr - rs
        note = ""
        if z == "GB":
            note = "excluded — gb_embedded's residual already absorbs it"
            mr = rs = 0.0
            drop = e_flat + e_oth
        elif ratio >= _SOLAR_DAY_NIGHT:
            note = f"SOLAR-shaped (d/n {ratio:.2f}) — variable part credited to must-take"
        elif e_oth > 0.5:
            note = "price-following — variable part left out (no SRMC)"
        print(f"{z:<10}{e_flat:>8.1f}{e_oth:>8.1f}{ratio:>6.2f}{'  ':<2}"
              f"{mr:>11.1f}{rs:>9.1f}{drop:>9.1f}   {note}")
        for k, v in zip(tot, (e_flat, e_oth, mr, rs, drop)):
            tot[k] += v
    print(f"\n{'TOTAL':<10}{tot['flat']:>8.1f}{tot['other']:>8.1f}{'':>6}{'  ':<2}"
          f"{tot['mustrun']:>11.1f}{tot['res']:>9.1f}{tot['drop']:>9.1f}")
    print(f"\n  recovered {tot['mustrun'] + tot['res']:.1f} TWh of {tot['flat'] + tot['other']:.1f} "
          f"({100 * (tot['mustrun'] + tot['res']) / max(tot['flat'] + tot['other'], 1e-9):.0f} %); "
          f"{tot['drop']:.1f} TWh still unrepresented, by the decisions in the module docstring")

    print("\n=== NL detail: the decomposition that matters ===")
    comp = components(cfg, ["NL"], YEAR)
    if comp is not None:
        idx = pd.DatetimeIndex(comp.index)
        print(f"  must-run floor  {comp['mustrun_mw'].mean() / 1000:6.2f} GW mean   "
              f"{comp['mustrun_mw'].sum() / 1e6:6.1f} TWh")
        print(f"  solar (in RES)  {comp['res_mw'].mean() / 1000:6.2f} GW mean   "
              f"{comp['res_mw'].sum() / 1e6:6.1f} TWh   peak {comp['res_mw'].max() / 1000:5.2f} GW")
        d = comp.groupby(idx.hour)["res_mw"].mean() / 1000
        print("  solar part by UTC hour (GW): " + " ".join(f"{v:.1f}" for v in d))
        print("  -> Dutch PV generated ~21 TWh in 2024 (IRENA/Ember); the residual gap is "
              "self-consumption that is never metered.")


if __name__ == "__main__":
    main()
