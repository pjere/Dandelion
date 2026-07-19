"""Métadonnées des stations SYNOP -> table dim_station.

Le fichier historique postesSynop.csv de Météo-France n'est plus servi (404). On utilise donc
la version Opendatasoft du jeu officiel « Données SYNOP essentielles OMM », qui expose pour chaque
station : nom, latitude, longitude, altitude, département et région. Repli sur les station_id
observés si la source est indisponible.
"""
from __future__ import annotations

import pandas as pd
import requests
from sqlalchemy import text
from sqlalchemy.engine import Engine

from ..db import upsert_df

ODS_URL = (
    "https://public.opendatasoft.com/api/explore/v2.1/catalog/datasets/"
    "donnees-synop-essentielles-omm/records"
)
ODS_SELECT = "numer_sta,nom,latitude,longitude,altitude,nom_dept,nom_reg"


class StationsUnavailableError(RuntimeError):
    pass


def fetch_stations() -> pd.DataFrame:
    """Récupère la liste des stations SYNOP (avec coordonnées + dépt/région) via Opendatasoft."""
    resp = requests.get(
        ODS_URL,
        params={"select": ODS_SELECT, "group_by": ODS_SELECT, "limit": 100},
        timeout=60,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if not results:
        raise StationsUnavailableError("Aucune station renvoyée par l'API Opendatasoft.")
    df = pd.DataFrame(results)
    df = df.rename(columns={
        "numer_sta": "station_id", "nom": "name",
        "nom_dept": "department", "nom_reg": "region",
    })
    df["station_id"] = df["station_id"].astype(str).str.zfill(5)
    keep = ["station_id", "name", "latitude", "longitude", "altitude", "department", "region"]
    return df[[c for c in keep if c in df.columns]].drop_duplicates(subset=["station_id"])


def _stations_from_obs(engine: Engine) -> pd.DataFrame:
    """Repli : station_id distincts déjà présents dans synop_obs (sans métadonnées)."""
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT DISTINCT station_id FROM synop_obs")).fetchall()
    return pd.DataFrame(
        [{"station_id": r[0], "name": None, "latitude": None, "longitude": None,
          "altitude": None, "department": None, "region": None} for r in rows]
    )


def load_stations(engine: Engine) -> tuple[int, str]:
    """Remplit dim_station. Retourne (nb_lignes, source) où source = 'opendatasoft' | 'observed'."""
    try:
        df = fetch_stations()
        source = "opendatasoft"
    except (StationsUnavailableError, requests.RequestException, ValueError):
        df = _stations_from_obs(engine)
        source = "observed"
    if df.empty:
        return 0, source
    # recrée la table pour garantir le schéma courant (ajout dépt/région)
    from ..db import METADATA, dim_station
    dim_station.drop(engine, checkfirst=True)
    METADATA.create_all(engine, tables=[dim_station])
    return upsert_df(engine, "dim_station", df, ["station_id"]), source
