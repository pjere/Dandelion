"""REPD (UK gov) → canonical `plant_registry` for GB (ADR-7).

The Renewable Energy Planning Database tracks GB renewable projects >150 kW through planning. We keep the
**Operational** rows in England/Scotland/Wales (Northern Ireland is in the all-island SEM, not GB). The
quarterly CSV asset URL rotates, so it is discovered from the gov.uk publication page and cached raw.

Scheme, per GB support history:
  * `roc` (Renewables Obligation, pre-~2017) — ROCs paid per MWh **regardless of price** ⇒ deep floor,
    no negative-price trigger; ~20-yr accreditation.
  * `cfd` (Contracts for Difference) — identified directly by the `CfD Capacity (MW)` column; the CfD has a
    **6-hour** negative-price rule ⇒ ≈0 floor past the run; 15-yr term.
  * `merchant` — subsidy-free / post-support (bids ≈0).

Data-quality vs MaStR/ODRÉ: the >150 kW threshold drops small rooftop solar (a modest share of GB solar),
and there is no CHP flag.
"""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pandas as pd

SOURCE = "repd"
ZONE = "GB"
DEFAULT_RAW = Path("data/raw/repd/repd.csv")
PUBLICATION = "https://www.gov.uk/government/publications/renewable-energy-planning-database-monthly-extract"

_TECH = {"Solar Photovoltaics": "solar", "Wind Onshore": "wind_onshore",
         "Wind Offshore": "wind_offshore", "Biomass (dedicated)": "biomass",
         "Biomass (co-firing)": "biomass", "Anaerobic Digestion": "biomass",
         "EfW Incineration": "waste", "Landfill Gas": "biomass", "Sewage Sludge Digestion": "biomass",
         "Hydro": "hydro_ror", "Small Hydro": "hydro_ror"}
_RES = {"solar", "wind_onshore", "wind_offshore", "biomass", "hydro_ror"}
_TERM = {"roc": 20, "cfd": 15, "merchant": 20}


def download(dest: str | Path = DEFAULT_RAW, timeout: int = 180) -> Path:
    """Discover the current REPD CSV asset from the gov.uk publication page and cache it raw."""
    import re

    import requests
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    page = requests.get(PUBLICATION, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"}).text
    m = re.search(r"https://assets\.publishing\.service\.gov\.uk/media/[a-f0-9]+/[^\"']+\.csv", page)
    if not m:
        raise RuntimeError("could not find the REPD CSV asset URL on the publication page")
    r = requests.get(m.group(0), timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    dest.write_bytes(r.content)
    return dest


def derive_scheme(commissioning: pd.Series, cfd_mw: pd.Series) -> pd.Series:
    """CfD where the CfD-capacity column is populated; else ROC pre-2017, merchant after."""
    yr = pd.to_datetime(commissioning, errors="coerce", utc=True).dt.year
    has_cfd = pd.to_numeric(cfd_mw, errors="coerce").fillna(0) > 0
    scheme = pd.Series("merchant", index=yr.index, dtype="object")
    scheme[yr < 2017] = "roc"
    scheme[has_cfd] = "cfd"
    scheme[yr.isna()] = pd.NA
    return scheme


def build(path: str | Path = DEFAULT_RAW) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"REPD landing zone not found: {path} (run repd.download())")
    df = pd.read_csv(io.BytesIO(path.read_bytes()), encoding="cp1252", low_memory=False)

    status = df.get("Development Status (short)").astype(str)
    country = df.get("Country").astype(str)
    keep = status.str.contains("Operational", case=False, na=False) & (country != "Northern Ireland")
    df = df[keep].copy()

    tech = df["Technology Type"].map(_TECH).fillna("other")
    comm = pd.to_datetime(df["Operational"], dayfirst=True, errors="coerce", utc=True)
    scheme = derive_scheme(comm, df.get("CfD Capacity (MW)"))
    scheme = scheme.where(tech.isin(_RES), pd.NA)
    support_end = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns, UTC]")
    for s, t in _TERM.items():
        mask = scheme == s
        support_end[mask] = comm[mask] + pd.DateOffset(years=t)

    ref = df.get("Ref ID").astype("string") if "Ref ID" in df else pd.Series(range(len(df))).astype("string")
    out = pd.DataFrame({
        "source": SOURCE, "source_id": ref, "zone": ZONE,
        "tech": tech, "fuel": df["Technology Type"],
        "capacity_mw": pd.to_numeric(df.get("Installed Capacity (MWelec)"), errors="coerce"),
        "commissioning_date": comm, "retirement_date": pd.NaT,
        "chp_flag": False, "chp_el_mw": np.nan,
        "scheme": scheme, "support_end": support_end,
        "status": "Operational",
    })
    return out[out["tech"] != "other"].reset_index(drop=True)      # drop battery/non-generation
