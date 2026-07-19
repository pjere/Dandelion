"""Cohort-level registry rows for zones without an open per-plant feed (ADR-7, graceful degradation).

BE (VREG/CWaPE), IT (GSE) and ES (RAIPRE) have no MaStR/ODRÉ-quality open plant download. For the RES bid
stack, `scheme_shares` only needs capacity by **(vintage, scheme)** — not individual plants — so a coarse
cohort table is enough: one canonical registry row per (zone, tech, vintage, scheme), with the cohort's
capacity, a representative commissioning year and `support_end = vintage + term`. It flows through the
identical `scheme_shares`/roll-off machinery as the plant-level sources.

The cohorts live in the editable `dispatch_res_vintages` workbook tab, seeded with **sourced national
aggregates** (round, explicitly low-confidence — the famous 2011 IT Conto-Energia solar boom, ES's 2020
merchant-solar surge, BE green certificates). They are estimates to be refined, not measurements.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

SOURCE = "cohort"

# support term (years) by scheme, for support_end = vintage + term
_TERM = {"green_certificate": 15, "conto_energia": 20, "cfd": 15, "recore": 20,
         "obligation_achat": 20, "merchant": 0, "fit": 20}


def build(workbook) -> pd.DataFrame:
    """`dispatch_res_vintages` tab → canonical registry rows (one per cohort)."""
    from powersim_core.scenario import load_sheet
    try:
        df = load_sheet(workbook, "dispatch", "res_vintages")
    except (ValueError, KeyError):
        return pd.DataFrame()
    comm = pd.to_datetime(df["vintage_year"].astype(int).astype(str) + "-07-01", utc=True)
    term = df["scheme"].map(_TERM).fillna(20)
    support_end = pd.to_datetime(
        [c + pd.DateOffset(years=int(t)) for c, t in zip(comm, term, strict=False)], utc=True)
    out = pd.DataFrame({
        "source": SOURCE,
        "source_id": (df["zone"].astype(str) + ":" + df["tech"].astype(str) + ":"
                      + df["vintage_year"].astype(int).astype(str) + ":" + df["scheme"].astype(str)),
        "zone": df["zone"], "tech": df["tech"], "fuel": df["tech"],
        "capacity_mw": pd.to_numeric(df["capacity_mw"], errors="coerce"),
        "commissioning_date": comm, "retirement_date": pd.NaT,
        "chp_flag": False, "chp_el_mw": np.nan,
        "scheme": df["scheme"], "support_end": support_end,
        "status": "cohort",
    })
    return out
