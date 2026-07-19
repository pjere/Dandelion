"""ODRÉ (RTE) → canonical `plant_registry` for France (ADR-7).

France's national production/storage registry (`registre national`, RTE via data.gouv.fr / ODRÉ
Opendatasoft). Per-installation for ≥36 kW; <36 kW is published aggregated by commune (a documented
lower-resolution tail). Downloaded whole (~137k rows, ~8 MB parquet) to a raw landing zone.

Like MaStR, ODRÉ does **not** state the support scheme — `regime` is operational status ("En service").
So the scheme (obligation d'achat vs complément de rémunération) is **derived statutorily**:
  * `obligation_achat` (FiT, guichet ouvert / old contracts) — paid ≈ regardless of price ⇒ deep floor.
  * `complément de rémunération` (tenders since ~2016) — its clause **suspends payment during negative
    hours** ⇒ bids ≈ 0 at negative prices.
  * `merchant` once commissioning + support term is reached (applied per projection year downstream).

Data-quality vs MaStR: good but the <36 kW aggregate rows carry a representative (not per-plant) vintage,
and there is no CHP flag here, so France's thermal must-run stays on the ENTSO-E/workbook path.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

SOURCE = "odre"
ZONE = "FR"
DATASET = "registre-national-installation-production-stockage-electricite-agrege"
EXPORT_URL = f"https://odre.opendatasoft.com/api/explore/v2.1/catalog/datasets/{DATASET}/exports/parquet"
DEFAULT_RAW = Path("data/raw/odre/registre_national.parquet")
SUPPORT_TERM_YEARS = 20

# filière → canonical tech (offshore split via codetechnologie below)
_FILIERE_TECH = {
    "Solaire": "solar", "Eolien": "wind_onshore", "Éolien": "wind_onshore",
    "Hydraulique": "hydro_ror", "Bioénergies": "biomass", "Bioenergies": "biomass",
    "Thermique non renouvelable": "gas", "Nucléaire": "nuclear", "Nucleaire": "nuclear",
}
_RES_TECHS = {"solar", "wind_onshore", "wind_offshore", "biomass", "hydro_ror"}


def download(dest: str | Path = DEFAULT_RAW, timeout: int = 300) -> Path:
    """Fetch the whole ODRÉ registry parquet export to the raw landing zone."""
    import requests
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(EXPORT_URL, timeout=timeout, stream=True) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return dest


def derive_scheme(commissioning: pd.Series, capacity_mw: pd.Series) -> pd.Series:
    """French support scheme by statutory rule (ODRÉ doesn't label it).

    <2016 → obligation d'achat (FiT era). 2016+: small (≤100 kW, guichet ouvert) → obligation d'achat;
    larger (tenders) → complément de rémunération. (`support_end` retires to merchant downstream.)
    """
    yr = pd.to_datetime(commissioning, errors="coerce", utc=True).dt.year
    kw = pd.to_numeric(capacity_mw, errors="coerce") * 1000.0
    scheme = pd.Series("obligation_achat", index=yr.index, dtype="object")
    scheme[(yr >= 2016) & (kw > 100)] = "complement_remuneration"
    scheme[yr.isna()] = pd.NA
    return scheme


def build(path: str | Path = DEFAULT_RAW) -> pd.DataFrame:
    """Vendor dump → canonical registry rows for FR (RES schemed; thermal/nuclear tech-only)."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"ODRÉ landing zone not found: {path} (run odre.download())")
    df = pd.read_parquet(path)

    comm = pd.to_datetime(df.get("datemiseenservice_date"), errors="coerce", utc=True)
    cap_mw = pd.to_numeric(df.get("puismaxinstallee"), errors="coerce") / 1000.0   # kW → MW

    tech = df.get("filiere").map(_FILIERE_TECH).fillna("other")
    codet = df.get("codetechnologie").astype(str)
    tech = tech.mask(codet.str.contains("EOLME|MARINE|OFFSHORE", case=False, na=False), "wind_offshore")

    scheme = derive_scheme(comm, cap_mw)
    scheme = scheme.where(tech.isin(_RES_TECHS), pd.NA)             # schemes only make sense for RES
    support_end = comm + pd.DateOffset(years=SUPPORT_TERM_YEARS)
    support_end = support_end.where(tech.isin(_RES_TECHS), pd.NaT)

    eic = df.get("codeeicresourceobject").astype("string")
    fallback = ("odre_" + df.get("nominstallation").astype("string").fillna("?") + "_"
                + df.get("codeinseecommune").astype("string").fillna("?"))
    source_id = eic.where(eic.notna() & (eic.str.len() > 0), fallback)

    out = pd.DataFrame({
        "source": SOURCE, "source_id": source_id, "zone": ZONE,
        "tech": tech, "fuel": df.get("filiere"),
        "capacity_mw": cap_mw, "commissioning_date": comm,
        "retirement_date": pd.NaT, "chp_flag": False, "chp_el_mw": np.nan,
        "scheme": scheme, "support_end": support_end,
        "status": df.get("regime"),
    })
    return out
