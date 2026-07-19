"""MaStR (Marktstammdatenregister) → canonical `plant_registry` (ADR-7).

Germany's plant-level registry, via `open-mastr`'s bulk download into a raw SQLite landing zone under
`~/.open-MaStR/` (the same role `data/raw/rte` plays for the RTE extract). This module is the ETL that
turns that vendor dump into canonical registry rows; it never edits the dump.

Three things MaStR gives us that no aggregate source can:

1. **CHP (`kwk`)** — `ElektrischeKwkLeistung` per unit. Must-run is physically driven by heat obligation,
   so this replaces the invented `must_run_frac` literature guesses (lignite .45 / coal .35 / gas .15).
2. **Reserve status** — `NetzreserveAbDatum` / `SicherheitsbereitschaftAbDatum`. German lignite was moved
   into *Sicherheitsbereitschaft* (security standby) and grid reserve from 2016-19: those units still sit
   in **installed capacity but cannot be dispatched into the market**. An ENTSO-E-capacity stack silently
   includes them → overstated lignite → inflated must-run → depressed prices. Only visible unit-level.
3. **EEG vintage** — `EegInbetriebnahmedatum` + `AusschreibungZuschlag` → scheme by statutory rule, and
   `+20 y` → `support_end`, which is what makes the negative-price bid stack roll off over time.

MaStR carries **no efficiency**; `efficiency_est` stays NA here and is modelled downstream.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

SOURCE = "mastr"
ZONE = "DE_LU"          # MaStR covers the German part of the DE-LU bidding zone

DEFAULT_DB = Path(os.path.expanduser("~/.open-MaStR/data/sqlite/open-mastr.db"))
XML_DIR = Path(os.path.expanduser("~/.open-MaStR/data/xml_download"))
BULK_URL = "https://download.marktstammdatenregister.de/Gesamtdatenexport_{date}_{version}.zip"


def fetch_bulk(date: str, version: str = "26.1", chunk_mb: int = 8, attempts: int = 12) -> Path:
    """Resumably download the ~3 GB MaStR bulk export → the path `open-mastr`'s parser expects.

    `open-mastr`'s own downloader streams the whole zip in one shot with no resume, so a single dropped
    connection loses the lot — it failed twice here (`ChunkedEncodingError`, then a transient DNS blip)
    after 233-310 MB. The server sends `Accept-Ranges: bytes`, so we resume from wherever we got to
    instead of restarting. Returns the zip path; `load_bulk_to_sqlite` then parses it.
    """
    import requests

    XML_DIR.mkdir(parents=True, exist_ok=True)
    dst = XML_DIR / f"Gesamtdatenexport_{date}.zip"          # name the parser looks for (no version suffix)
    url = BULK_URL.format(date=date, version=version)
    total = int(requests.head(url, timeout=60, allow_redirects=True).headers.get("Content-Length", 0))

    for attempt in range(1, attempts + 1):
        have = dst.stat().st_size if dst.exists() else 0
        if total and have >= total:
            print(f"[mastr] complete: {have/1e9:.2f} GB")
            return dst
        headers = {"User-Agent": "powersim/1.0", "Range": f"bytes={have}-"} if have else {"User-Agent": "powersim/1.0"}
        try:
            with requests.get(url, headers=headers, stream=True, timeout=(30, 120)) as r:
                r.raise_for_status()
                with dst.open("ab" if have else "wb") as fh:
                    for chunk in r.iter_content(chunk_size=chunk_mb * 1024 * 1024):
                        fh.write(chunk)
        except Exception as e:                                # noqa: BLE001 — any network fault ⇒ resume
            got = dst.stat().st_size if dst.exists() else 0
            print(f"[mastr] attempt {attempt}: {type(e).__name__} at {got/1e9:.2f}/{total/1e9:.2f} GB — resuming")
            continue
    raise RuntimeError(f"MaStR bulk download did not complete after {attempts} attempts")


def load_bulk_to_sqlite(date: str, data: list[str], bulk_cleansing: bool = True) -> None:
    """Parse an already-downloaded bulk zip into the open-mastr SQLite landing zone."""
    from open_mastr import Mastr
    from open_mastr.xml_download.utils_write_to_database import write_mastr_xml_to_database

    db = Mastr(engine="sqlite")
    write_mastr_xml_to_database(engine=db.engine,
                                zipped_xml_file_path=str(XML_DIR / f"Gesamtdatenexport_{date}.zip"),
                                data=data, bulk_cleansing=bulk_cleansing, bulk_download_date=date)

# Energietraeger (MaStR) → our canonical tech
_FUEL_TECH = {
    "erdgas": "gas", "steinkohle": "coal", "braunkohle": "lignite",
    "mineralölprodukte": "oil", "mineraloelprodukte": "oil", "heizöl": "oil",
    "kernenergie": "nuclear", "biomasse": "biomass", "deponiegas": "biomass",
    "klärgas": "biomass", "grubengas": "gas", "andere gase": "gas", "abfall": "waste",
    "wärme": "other", "nicht biogener abfall": "waste",
}
_UNIT_TABLES = {                     # table -> default tech when Energietraeger is absent/uniform
    "combustion_extended": None,     # tech from Energietraeger
    "nuclear_extended": "nuclear",
    "biomass_extended": "biomass",
    "hydro_extended": "hydro_ror",   # refined below via Technologie
    "solar_extended": "solar",
    "wind_extended": "wind_onshore",  # refined below via Lage
    "storage_extended": "hydro_psp",
}


def _read(con, table: str, cols: list[str]) -> pd.DataFrame:
    """Read only the columns that exist (MaStR's schema varies a little by technology table)."""
    have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    use = [c for c in cols if c in have]
    if not use:
        return pd.DataFrame(columns=cols)
    select = ", ".join(f'"{c}"' for c in use)
    return pd.read_sql(f'SELECT {select} FROM "{table}"', con)


def _map_tech(row) -> str:
    et = str(row.get("Energietraeger") or "").strip().lower()
    return _FUEL_TECH.get(et, "other")


def load_chp(con) -> pd.DataFrame:
    """CHP register keyed by `KwkMastrNummer` → electrical CHP capacity (the must-run driver)."""
    df = _read(con, "kwk", ["KwkMastrNummer", "ElektrischeKwkLeistung", "ThermischeNutzleistung",
                            "AnlageBetriebsstatus"])
    if df.empty:
        return df
    df["ElektrischeKwkLeistung"] = pd.to_numeric(df["ElektrischeKwkLeistung"], errors="coerce")
    return df.dropna(subset=["KwkMastrNummer"]).drop_duplicates("KwkMastrNummer")


def _reserve_flag(df: pd.DataFrame) -> pd.Series:
    """True where the unit has been moved to grid reserve / security standby ⇒ NOT market-dispatchable."""
    out = pd.Series(False, index=df.index)
    for c in ("NetzreserveAbDatum", "SicherheitsbereitschaftAbDatum", "DatumUeberfuehrungInReserve"):
        if c in df:
            out = out | pd.to_datetime(df[c], errors="coerce").notna()
    return out


_UNIT_COLS = [
    "EinheitMastrNummer", "NameStromerzeugungseinheit", "NameKraftwerk", "NameKraftwerksblock",
    "Energietraeger", "Technologie", "Nettonennleistung", "Bruttoleistung",
    "Inbetriebnahmedatum", "DatumEndgueltigeStilllegung", "EinheitBetriebsstatus",
    "KwkMastrNummer", "EegMastrNummer", "Bundesland", "Laengengrad", "Breitengrad",
    "NetzreserveAbDatum", "SicherheitsbereitschaftAbDatum", "DatumUeberfuehrungInReserve", "Lage",
]

# EEG support term (years) — commissioning + 20y ⇒ roll-off to merchant (first expiries hit 1-Jan-2021)
EEG_TERM_YEARS = 20


def derive_scheme(commissioning: pd.Series, capacity_mw: pd.Series, auction: pd.Series) -> pd.Series:
    """Support scheme by **statutory rule** — MaStR does not label it.

    <2012 → FiT · 2012-15 & >500 kW → market premium · 2016+ & >100 kW → market premium (mandatory
    direct marketing) · <100 kW → FiT · any auction award → market premium at the auction strike.
    (`support_end` then retires the row to `merchant`; applied in `build`.)
    """
    yr = pd.to_datetime(commissioning, errors="coerce").dt.year
    kw = pd.to_numeric(capacity_mw, errors="coerce") * 1000.0
    scheme = pd.Series("fit", index=yr.index, dtype="object")
    scheme[(yr.between(2012, 2015)) & (kw > 500)] = "market_premium"
    scheme[(yr >= 2016) & (kw > 100)] = "market_premium"
    scheme[auction.fillna(False).astype(bool)] = "market_premium"
    scheme[yr.isna()] = pd.NA
    return scheme


# the conventional/dispatchable fleet — all we need for the DE merit order (thermal + nuclear).
THERMAL_TABLES = ["combustion_extended", "nuclear_extended"]


def build(db_path: str | Path = DEFAULT_DB, as_of: str | None = None,
          tables: list[str] | None = None) -> pd.DataFrame:
    """Vendor dump → canonical registry rows for DE_LU (one row per generating unit).

    `tables` restricts the source tables (default: all of `_UNIT_TABLES`). Pass `THERMAL_TABLES` to
    build just the dispatchable merit order without walking the millions of PV/battery rows.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"MaStR landing zone not found: {db_path} (run the open-mastr download)")
    want = tables or list(_UNIT_TABLES)
    con = sqlite3.connect(db_path)
    try:
        chp = load_chp(con)
        chp_cap = dict(zip(chp.get("KwkMastrNummer", []), chp.get("ElektrischeKwkLeistung", [])))
        eeg = _eeg_index(con) if any(t not in THERMAL_TABLES for t in want) else pd.DataFrame()
        frames = []
        for table in want:
            df = _read(con, table, _UNIT_COLS)
            if df.empty:
                continue
            frames.append(_to_canonical(df, table, _UNIT_TABLES.get(table), chp_cap, eeg, as_of))
    finally:
        con.close()
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return out


def _eeg_index(con) -> pd.DataFrame:
    """EEG rows across technologies, keyed by EegMastrNummer → vintage + auction flag."""
    cols = ["EegMastrNummer", "EegInbetriebnahmedatum", "InstallierteLeistung",
            "AusschreibungZuschlag", "Zuschlagsnummer"]
    frames = [_read(con, t, cols) for t in ("solar_eeg", "wind_eeg", "biomass_eeg", "hydro_eeg", "gsgk_eeg")]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame(columns=cols)
    df = pd.concat(frames, ignore_index=True).dropna(subset=["EegMastrNummer"])
    return df.drop_duplicates("EegMastrNummer").set_index("EegMastrNummer")


def _tech_of(df: pd.DataFrame, table: str, default_tech: str | None) -> pd.Series:
    if table == "combustion_extended":
        return df.apply(_map_tech, axis=1)
    if table == "wind_extended" and "Lage" in df:                      # onshore vs offshore
        offshore = df["Lage"].astype(str).str.contains("see", case=False, na=False)
        return pd.Series(default_tech, index=df.index).mask(offshore, "wind_offshore")
    if table == "hydro_extended" and "Technologie" in df:              # ROR vs reservoir vs PSP
        t = df["Technologie"].astype(str).str.lower()
        out = pd.Series("hydro_ror", index=df.index)
        out[t.str.contains("speicher", na=False)] = "hydro_reservoir"
        out[t.str.contains("pump", na=False)] = "hydro_psp"
        return out
    return pd.Series(default_tech, index=df.index)


def _allocate_chp(df: pd.DataFrame, cap_mw: pd.Series, chp_cap: dict) -> pd.Series:
    """Per-unit CHP electrical capacity (MW), the must-run driver.

    A `KwkMastrNummer` is a **CHP-plant** registration that can link several generating units, so mapping
    each unit to the plant's full `ElektrischeKwkLeistung` double-counts badly (German gas alone has ~70k
    small CHP units sharing registrations → a naive join gave 58 GW of "CHP" against 36 GW installed).
    Allocate each registration's electrical capacity across its linked units in proportion to unit size,
    capped at the unit's own capacity, so Σ per registration is conserved.

    NB: this is CHP *capacity*, not an hourly must-run — heat obligation is seasonal (high winter, ~0
    summer). The dispatch layer applies a heat-demand shape on top; see docs/RES_BIDDING_DESIGN.md.
    """
    if "KwkMastrNummer" not in df:
        return pd.Series(np.nan, index=df.index, dtype="float")
    kwk_no = df["KwkMastrNummer"]
    reg_mw = pd.to_numeric(kwk_no.map(chp_cap), errors="coerce") / 1000.0            # kW → MW, per plant
    cap = cap_mw.fillna(0.0)
    grp_cap = cap.groupby(kwk_no).transform("sum")
    share = (cap / grp_cap).where(grp_cap > 0, 0.0)
    alloc = np.minimum(cap, reg_mw * share)
    return alloc.where(kwk_no.notna() & (reg_mw > 0))


def _to_canonical(df, table, default_tech, chp_cap, eeg, as_of) -> pd.DataFrame:
    cap_mw = pd.to_numeric(df.get("Nettonennleistung"), errors="coerce") / 1000.0   # kW → MW
    commissioning = pd.to_datetime(df.get("Inbetriebnahmedatum"), errors="coerce")

    # EEG vintage: the EEG record's own commissioning drives the 20-year term (it can differ from the
    # unit's technical commissioning, e.g. after repowering). Join by value with `map` (reindex on a
    # Series mis-aligns against the multi-million-row EEG index).
    eeg_no = df.get("EegMastrNummer")
    if eeg_no is None or not len(eeg):
        eeg_comm = pd.Series(pd.NaT, index=df.index)
        auction = pd.Series(False, index=df.index)
    else:
        eeg_comm = pd.to_datetime(eeg_no.map(eeg["EegInbetriebnahmedatum"]), errors="coerce")
        auction = eeg_no.map(eeg["AusschreibungZuschlag"])

    support_start = eeg_comm.fillna(commissioning)
    support_end = support_start + pd.DateOffset(years=EEG_TERM_YEARS)
    # Store the **statutory** scheme (observed truth) + `support_end`. The roll-off to merchant is a
    # *time-dependent* transition (a unit is merchant once `support_end <= projection_year`) and belongs
    # in the scheme-evolution model, NOT baked here against today's date — otherwise a 2040 projection
    # would see the 2026-snapshot's merchant share instead of 2040's. (ADR-7: registry = immutable truth.)
    scheme = derive_scheme(support_start, cap_mw, auction)
    scheme = scheme.mask(support_start.isna(), pd.NA)      # non-EEG (conventional) units have no scheme

    chp_el = _allocate_chp(df, cap_mw, chp_cap)

    out = pd.DataFrame({
        "source": SOURCE,
        "source_id": df.get("EinheitMastrNummer"),
        "as_of": as_of or pd.Timestamp.utcnow().normalize(),
        "zone": ZONE,
        "tech": _tech_of(df, table, default_tech),
        "fuel": df.get("Energietraeger"),
        "capacity_mw": cap_mw,
        "commissioning_date": commissioning,
        "retirement_date": pd.to_datetime(df.get("DatumEndgueltigeStilllegung"), errors="coerce"),
        "chp_flag": chp_el.notna() & (chp_el > 0),
        "scheme": scheme,
        "support_end": support_end,
        "lat": pd.to_numeric(df.get("Breitengrad"), errors="coerce"),
        "lon": pd.to_numeric(df.get("Laengengrad"), errors="coerce"),
    })
    # extras the dispatch stack needs; carried alongside the canonical schema
    out["chp_el_mw"] = chp_el
    out["in_reserve"] = _reserve_flag(df)          # grid reserve / Sicherheitsbereitschaft ⇒ not market
    out["status"] = df.get("EinheitBetriebsstatus")
    out["name"] = df.get("NameKraftwerk").fillna(df.get("NameStromerzeugungseinheit")) \
        if "NameKraftwerk" in df else df.get("NameStromerzeugungseinheit")
    return out
