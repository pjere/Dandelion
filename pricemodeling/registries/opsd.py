"""OPSD `renewable_power_plants` → canonical `plant_registry` for CH (ADR-7).

Open Power System Data's harmonised renewable registry. Used here for **Switzerland**, which OPSD covers
with `commissioning_date`, `contract_period_end` (the Pronovo/KEV support end — direct, no derivation) and
the KEV tariff. Frozen at the 2020-08-25 vintage, so it misses 2020+ additions — acceptable for CH, a
small, slow-growing RES fleet whose negatives are ~all imported from the coupling.

Scheme: Switzerland ran **KEV** (Kostendeckende Einspeisevergütung — a feed-in tariff paid ≈regardless of
price ⇒ deep floor, no §51 trigger; CH is not in the EU EEG). A plant with a `contract_period_end` is KEV
until that date, then `merchant`; plants without KEV support are merchant.
"""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pandas as pd

SOURCE = "opsd_ch"
ZONE = "CH"
URL = "https://data.open-power-system-data.org/renewable_power_plants/2020-08-25/renewable_power_plants_CH.csv"
DEFAULT_RAW = Path("data/raw/opsd/ch.csv")

_TECH = {"Solar": "solar", "Wind": "wind_onshore", "Hydro": "hydro_ror",
         "Bioenergy": "biomass", "Geothermal": "biomass"}
_RES = {"solar", "wind_onshore", "biomass", "hydro_ror"}


def download(dest: str | Path = DEFAULT_RAW, timeout: int = 180) -> Path:
    import requests
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(URL, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    dest.write_bytes(r.content)
    return dest


def build(path: str | Path = DEFAULT_RAW) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"OPSD-CH landing zone not found: {path} (run opsd.download())")
    df = pd.read_csv(io.BytesIO(path.read_bytes()), low_memory=False)

    tech = df.get("energy_source_level_2").map(_TECH).fillna("other")
    comm = pd.to_datetime(df.get("commissioning_date"), errors="coerce", utc=True)
    support_end = pd.to_datetime(df.get("contract_period_end"), errors="coerce", utc=True)
    support_end = support_end.fillna(comm + pd.DateOffset(years=20))
    scheme = pd.Series("kev", index=df.index, dtype="object").where(
        df.get("contract_period_end").notna(), "merchant")
    scheme = scheme.where(tech.isin(_RES), pd.NA)
    support_end = support_end.where(tech.isin(_RES), pd.NaT)

    out = pd.DataFrame({
        "source": SOURCE, "source_id": pd.Series(range(len(df))).astype("string"), "zone": ZONE,
        "tech": tech, "fuel": df.get("energy_source_level_2"),
        "capacity_mw": pd.to_numeric(df.get("electrical_capacity"), errors="coerce"),
        "commissioning_date": comm, "retirement_date": pd.NaT,
        "chp_flag": False, "chp_el_mw": np.nan,
        "scheme": scheme, "support_end": support_end,
        "lat": pd.to_numeric(df.get("lat"), errors="coerce"),
        "lon": pd.to_numeric(df.get("lon"), errors="coerce"),
        "status": "operational",
    })
    return out[out["tech"] != "other"].reset_index(drop=True)
