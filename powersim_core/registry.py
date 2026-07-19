"""The asset master — canonical `plant_registry` in the lake's `reference` layer (ADR-7).

One row per physical generating unit, one stable ID space, provenance stamped. This replaces asset data
scattered across the workbook (`avail_fleet_registry`), SQLite (`entsoe_installed_capacity`) and
dispatch's p99.9-of-generation hack.

**registry ≠ scenario.** The registry is *observed truth*: immutable, re-derivable from ETL, never edited
by hand. Scenario deltas (closures, new build, overrides) stay in `scenarios.xlsx`, and models read
`registry ⊕ overrides` via `apply_overrides`. Collapsing the two would either break the single-file
editing workflow (ADR-5) or corrupt source truth on every scenario tweak.

Vendor dumps (`~/.open-MaStR/`, ODRÉ CSVs, …) are raw landing zones — the same role `data/raw/rte` plays
for the RTE extract. They are not this.
"""
from __future__ import annotations

import pandas as pd

from . import lake

LAYER, DATASET = "reference", "plant_registry"

#: canonical columns. `plant_id` is `{source}:{source_id}` — stable, collision-free across registries.
COLUMNS = [
    "plant_id",            # stable unique id: f"{source}:{source_id}"
    "source",              # mastr | odre | vreg | ofgem | pronovo | gse | raipre | entsoe | workbook
    "source_id",           # the registry's own key (e.g. MaStR EinheitMastrNummer)
    "as_of",               # snapshot date of the source extract (provenance)
    "zone",                # dispatch zone (FR, DE_LU, …)
    "tech",                # nuclear|lignite|coal|gas|oil|biomass|hydro_*|solar|wind_onshore|wind_offshore
    "fuel",                # primary fuel where distinct from tech
    "capacity_mw",         # net electrical capacity
    "commissioning_date",
    "retirement_date",     # legislated/announced closure where known (per-unit coal phase-out)
    "chp_flag",            # KWK/cogeneration → heat-obligated ⇒ the physical driver of must-run
    "chp_el_mw",           # allocated CHP-electrical capacity → the must-run *level* (× seasonal heat shape)
    "status",              # operational status (e.g. "In Betrieb") — filters the live fleet
    "efficiency_est",      # MODELLED (vintage+tech+size). No registry carries efficiency.
    "scheme",              # support scheme, DERIVED by statutory rule (registries don't label it)
    "aw_eur_mwh",          # anzulegender Wert / auction strike → negative-price bid floor = -(AW - MW_month)
    "support_end",         # commissioning + term (DE: 20y) ⇒ roll-off to merchant
    "lat", "lon",
]

_NUMERIC = ["capacity_mw", "chp_el_mw", "efficiency_est", "aw_eur_mwh"]
_DATES = ["as_of", "commissioning_date", "retirement_date", "support_end"]


def empty() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in COLUMNS})


def normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce a source-specific frame to the canonical schema (missing columns → NA, extras dropped)."""
    out = df.copy()
    for c in COLUMNS:
        if c not in out:
            out[c] = pd.NA
    for c in _NUMERIC:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    for c in _DATES:
        out[c] = pd.to_datetime(out[c], errors="coerce", utc=True)
    if "chp_flag" in out:
        out["chp_flag"] = out["chp_flag"].astype("boolean")
    if out["plant_id"].isna().any():                       # derive the stable id where absent
        out["plant_id"] = out["source"].astype(str) + ":" + out["source_id"].astype(str)
    return out[COLUMNS]


def write(df: pd.DataFrame, source: str) -> object:
    """Write/replace one source's slice of the registry (partitioned by source → re-runnable per ETL)."""
    return lake.write_table(normalise(df), LAYER, DATASET, index=False, source=source)


def read(source: str | None = None, zone: str | None = None) -> pd.DataFrame:
    """Read the registry — one source's partition, or every source concatenated."""
    df = lake.read_table(LAYER, DATASET, source=source) if source else lake.read_table(LAYER, DATASET)
    return df[df["zone"] == zone] if zone else df


def apply_overrides(reg: pd.DataFrame, overrides: pd.DataFrame | None) -> pd.DataFrame:
    """`registry ⊕ overrides` — scenario deltas from the workbook applied over observed truth.

    Overrides carry `plant_id` plus any canonical column(s) to change; a row with `capacity_mw == 0`
    (or a `retirement_date` in the past) retires the unit. Rows whose `plant_id` is absent from the
    registry are treated as **new build** and appended.
    """
    if overrides is None or overrides.empty:
        return reg
    reg = reg.set_index("plant_id")
    ov = normalise(overrides).set_index("plant_id")
    known = ov.index.intersection(reg.index)
    for col in [c for c in COLUMNS if c != "plant_id"]:
        upd = ov.loc[known, col].dropna()
        if not upd.empty:
            reg.loc[upd.index, col] = upd
    new = ov.loc[ov.index.difference(reg.index)]
    out = pd.concat([reg, new]) if not new.empty else reg
    return out.reset_index()


def active(reg: pd.DataFrame, year: int) -> pd.DataFrame:
    """Units generating in `year` (commissioned by then, not yet retired)."""
    ts = pd.Timestamp(f"{year}-07-01", tz="UTC")
    live = (reg["commissioning_date"].isna()) | (reg["commissioning_date"] <= ts)
    dead = reg["retirement_date"].notna() & (reg["retirement_date"] <= ts)
    return reg[live & ~dead]
