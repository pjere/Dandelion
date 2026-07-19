"""Téléchargement et parsing des archives SYNOP mensuelles -> table synop_obs.

Source : https://donneespubliques.meteofrance.fr/donnees_libres/Txt/Synop/Archive/synop.YYYYMM.csv.gz
Format : CSV ';' , valeurs manquantes = 'mq', horodatage UTC (YYYYMMDDHHMMSS), pas de 3 h.
"""
from __future__ import annotations

import gzip
from datetime import date
from pathlib import Path

import pandas as pd
import requests
from sqlalchemy.engine import Engine

from ..db import already_ingested, log_ingest, upsert_df

ARCHIVE_URL = (
    "https://donneespubliques.meteofrance.fr/donnees_libres/Txt/Synop/Archive/synop.{ym}.csv.gz"
)

# Conversions d'unités appliquées aux codes SYNOP bruts vers l'unité « lisible ».
#   K -> °C : t, td   |   Pa -> hPa : pmer, pres
_KELVIN_TO_C = {"t", "td", "tn12", "tn24", "tx12", "tx24", "tminsol", "tw"}
_PA_TO_HPA = {"pmer", "pres", "tend", "tend24"}


def _convert(code: str, series: pd.Series) -> pd.Series:
    if code in _KELVIN_TO_C:
        return series - 273.15
    if code in _PA_TO_HPA:
        return series / 100.0
    return series


def months_between(start: date, end: date) -> list[str]:
    """Liste des 'YYYYMM' couvrant [start, end] inclus."""
    months = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append(f"{y:04d}{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return months


GZIP_MAGIC = b"\x1f\x8b"


def _is_gzip(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(2) == GZIP_MAGIC
    except OSError:
        return False


def download_month(ym: str, raw_dir: Path) -> Path | None:
    """Télécharge (avec cache) l'archive mensuelle. Retourne le chemin local, ou None si le
    mois est indisponible (404, ou le portail renvoie une page HTML au lieu du .gz)."""
    dest = raw_dir / "synop" / f"synop.{ym}.csv.gz"
    dest.parent.mkdir(parents=True, exist_ok=True)
    # cache valide uniquement si c'est bien un gzip
    if dest.exists() and dest.stat().st_size > 0 and _is_gzip(dest):
        return dest
    url = ARCHIVE_URL.format(ym=ym)
    resp = requests.get(url, timeout=120)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    # le portail renvoie parfois une page HTML (HTTP 200) pour un mois absent
    if resp.content[:2] != GZIP_MAGIC:
        return None
    dest.write_bytes(resp.content)
    return dest


def parse_month(
    path: Path, parameters: dict[str, str], stations: list[str] | None = None
) -> pd.DataFrame:
    """Parse une archive mensuelle en DataFrame long (station_id, ts_utc, variable, value)."""
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        df = pd.read_csv(fh, sep=";", na_values=["mq", ""], dtype={"numer_sta": str})
    if df.empty:
        return pd.DataFrame(columns=["station_id", "ts_utc", "variable", "value"])

    df["station_id"] = df["numer_sta"].astype(str).str.zfill(5)
    if stations:
        df = df[df["station_id"].isin(stations)]
    # Horodatage UTC
    ts = pd.to_datetime(df["date"].astype(str), format="%Y%m%d%H%M%S", utc=True, errors="coerce")
    df["ts_utc"] = ts.dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    df = df[df["ts_utc"].notna()]

    frames = []
    for code, friendly in parameters.items():
        if code not in df.columns:
            continue
        values = pd.to_numeric(df[code], errors="coerce")
        values = _convert(code, values)
        part = pd.DataFrame(
            {
                "station_id": df["station_id"].values,
                "ts_utc": df["ts_utc"].values,
                "variable": friendly,
                "value": values.values,
            }
        )
        frames.append(part.dropna(subset=["value"]))

    if not frames:
        return pd.DataFrame(columns=["station_id", "ts_utc", "variable", "value"])
    out = pd.concat(frames, ignore_index=True)
    return out.drop_duplicates(subset=["station_id", "ts_utc", "variable"])


def extract_synop(
    engine: Engine,
    raw_dir: Path,
    start: date,
    end: date,
    parameters: dict[str, str],
    stations: list[str] | None = None,
    force: bool = False,
) -> int:
    """Extrait toutes les archives mensuelles de la période vers synop_obs. Incrémental."""
    total = 0
    months = months_between(start, end)
    for ym in months:
        source, chunk_key = "synop", ym
        # On ré-extrait toujours le mois courant (données non figées), sinon on saute si déjà fait.
        is_current = ym == end.strftime("%Y%m")
        if not force and not is_current and already_ingested(engine, source, chunk_key):
            continue
        path = download_month(ym, raw_dir)
        if path is None:
            log_ingest(engine, source, chunk_key, 0, status="missing")
            continue
        try:
            df = parse_month(path, parameters, stations)
            rows = upsert_df(engine, "synop_obs", df, ["station_id", "ts_utc", "variable"])
        except Exception as exc:  # un mois corrompu ne doit pas tuer tout le pipeline
            log_ingest(engine, source, chunk_key, 0, status=f"error:{type(exc).__name__}")
            continue
        log_ingest(engine, source, chunk_key, rows)
        total += rows
    return total
