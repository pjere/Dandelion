"""Free/open commodity price series — the committed standalone fallback for the observed store.

So that a fresh clone can price historical SRMCs against *real* fuel rather than the annual-level ×
seasonal-shape trajectory (which, for 2022, inverted the year: ~145 €/MWh_th modelled in January against
~80 actual, and ~108 in August against ~235). Licensed data (Montel / Bloomberg / Refinitiv) is strictly
better and overrides these via `observed.ingest_csv`; this module exists so the repo works without it.

Sources (both open, no key):
  * **World Bank "Pink Sheet"** monthly commodity prices — `Natural gas, Europe` ($/mmbtu),
    `Coal, South African` ($/mt), `Crude oil, Brent` ($/bbl).
  * **ECB euro reference rates** — daily EUR/USD, monthly-averaged to convert the USD series.

Honest limitations, because these matter when reading a backtest:
  * the World Bank gas series is a **monthly index**, not a TTF day-ahead curve — it fixes the 2022 level
    inversion but cannot carry intra-month volatility (August 2022 traded intramonth to ~€340);
  * **API2 is not published here**; South African coal is a *proxy* with its own basis to ARA;
  * **EUA (carbon) is not available** from an open source we can redistribute — `co2` therefore stays on
    the scenario trajectory until a licensed series is ingested. Supply one with
    `ingest_csv(..., commodity="co2")` for a materially better thermal SRMC.
"""
from __future__ import annotations

import io
import pathlib
import zipfile

import pandas as pd

from .observed import normalise, write_observed

WORLDBANK_URL = ("https://thedocs.worldbank.org/en/doc/18675f1d1639c7a34d463f59263ba0a2-0050012025"
                 "/related/CMO-Historical-Data-Monthly.xlsx")
ECB_FX_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"
RAW_DIR = pathlib.Path("data/raw/commodities")          # git-ignored landing zone
_UA = {"User-Agent": "Mozilla/5.0 (powersim research)"}

MMBTU_PER_MWH = 0.293071                                 # 1 MMBtu = 0.293071 MWh
MWH_TH_PER_TONNE_COAL = 6.978                            # 6000 kcal/kg steam coal


def fetch(dest: pathlib.Path = RAW_DIR) -> dict[str, pathlib.Path]:
    """Download the raw public files into the git-ignored landing zone (idempotent)."""
    import requests
    dest.mkdir(parents=True, exist_ok=True)
    wb = dest / "worldbank_pinksheet_monthly.xlsx"
    if not wb.exists():
        r = requests.get(WORLDBANK_URL, timeout=120, headers=_UA); r.raise_for_status()
        wb.write_bytes(r.content)
    fx = dest / "ecb_eurofxref_hist.csv"
    if not fx.exists():
        r = requests.get(ECB_FX_URL, timeout=120, headers=_UA); r.raise_for_status()
        z = zipfile.ZipFile(io.BytesIO(r.content))
        fx.write_bytes(z.read(z.namelist()[0]))
    return {"worldbank": wb, "ecb_fx": fx}


def load_fx_monthly(path: pathlib.Path) -> pd.Series:
    """Monthly-average EUR/USD (USD per 1 EUR) from the ECB reference rates."""
    df = pd.read_csv(path)
    df.columns = [str(c).strip() for c in df.columns]
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    s = pd.to_numeric(df["USD"], errors="coerce")
    out = pd.DataFrame({"date": df["Date"], "usd_per_eur": s}).dropna()
    return out.set_index("date")["usd_per_eur"].resample("MS").mean()


def load_worldbank_monthly(path: pathlib.Path, fx_monthly: pd.Series) -> pd.DataFrame:
    """Pink Sheet → canonical [date, commodity, price]: gas/coal €/MWh_th, oil $/bbl."""
    raw = pd.read_excel(path, sheet_name="Monthly Prices", header=None, skiprows=4)
    names = [str(v).strip() for v in raw.iloc[0].tolist()]
    data = raw.iloc[2:].copy()
    data.columns = ["period"] + names[1:]
    data = data[data["period"].astype(str).str.match(r"^\d{4}M\d{2}$", na=False)]
    data["date"] = pd.to_datetime(data["period"].astype(str), format="%YM%m")

    def col(sub: str) -> str:
        hits = [c for c in data.columns if isinstance(c, str) and sub.lower() in c.lower()]
        if not hits:
            raise KeyError(f"World Bank column matching {sub!r} not found")
        return hits[0]

    d = data.set_index("date")
    fx = fx_monthly.reindex(d.index).ffill()
    gas_usd = pd.to_numeric(d[col("Natural gas, Europe")], errors="coerce")
    coal_usd = pd.to_numeric(d[col("Coal, South African")], errors="coerce")
    brent = pd.to_numeric(d[col("Crude oil, Brent")], errors="coerce")
    frames = [
        pd.DataFrame({"date": d.index, "commodity": "gas",
                      "price": gas_usd / fx / MMBTU_PER_MWH}),          # $/mmbtu -> EUR/MWh_th
        pd.DataFrame({"date": d.index, "commodity": "coal",
                      "price": coal_usd / fx / MWH_TH_PER_TONNE_COAL}),  # $/t -> EUR/MWh_th
        pd.DataFrame({"date": d.index, "commodity": "oil", "price": brent}),  # $/bbl is canonical
    ]
    return pd.concat(frames, ignore_index=True).dropna(subset=["price"])


def ingest_public(dest: pathlib.Path = RAW_DIR, since: str = "2014-01-01", write: bool = True):
    """Fetch (if needed), convert and store the public series under source='worldbank'."""
    paths = fetch(dest)
    df = load_worldbank_monthly(paths["worldbank"], load_fx_monthly(paths["ecb_fx"]))
    df = df[df["date"] >= pd.Timestamp(since)]
    out = normalise(df, source="worldbank", granularity="monthly")
    if write:
        write_observed(out, source="worldbank")
    return out
