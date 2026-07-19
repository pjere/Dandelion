"""Couche base de données : moteur SQLAlchemy, schéma SQLite et helpers d'upsert.

Choix de conception
-------------------
* Toutes les séries temporelles RTE sont normalisées dans un **schéma long commun**
  (``ts_utc, ts_end_utc, series_key, sub_key, label, value``), une table par ressource.
  Cela rend l'extracteur générique et robuste aux variations de schéma JSON entre années.
* La météo SYNOP est aussi stockée en long (``station_id, ts_utc, variable, value``).
* Les tables de dimension et la table maître sont définies explicitement.
* Horodatages stockés en **UTC**, format ISO 8601 (texte), pour rester portable SQLite.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC

import pandas as pd
from sqlalchemy import (
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    text,
)
from sqlalchemy.engine import Engine

METADATA = MetaData()

# Colonnes standard d'une série temporelle RTE normalisée.
RTE_TS_COLUMNS = ["ts_utc", "ts_end_utc", "series_key", "sub_key", "label", "value"]
RTE_TS_PK = ["ts_utc", "series_key", "sub_key"]

# ----- Dimensions & tables fixes -------------------------------------------

dim_station = Table(
    "dim_station", METADATA,
    Column("station_id", String, primary_key=True),
    Column("name", String),
    Column("latitude", Float),
    Column("longitude", Float),
    Column("altitude", Float),
    Column("department", String),
    Column("region", String),
)

synop_obs = Table(
    "synop_obs", METADATA,
    Column("station_id", String, primary_key=True),
    Column("ts_utc", String, primary_key=True),
    Column("variable", String, primary_key=True),
    Column("value", Float),
)

dim_production_unit = Table(
    "dim_production_unit", METADATA,
    Column("eic_code", String, primary_key=True),   # code EIC observé (clé stable inter-années)
    Column("canonical_eic", String),                # EIC canonique (= eic_code sauf fusion manuelle)
    Column("canonical_name", String),
    Column("fuel_type", String),
    Column("aliases", Text),        # JSON : liste des libellés observés
    Column("n_obs", Integer),       # nb d'observations (volumétrie)
    Column("first_seen", String),
    Column("last_seen", String),
    Column("match_source", String), # eic | fuzzy | override
)

fact_hourly_long = Table(
    "fact_hourly_long", METADATA,
    Column("ts_utc", String, primary_key=True),
    Column("source", String, primary_key=True),
    Column("variable", String, primary_key=True),
    Column("value", Float),
)

ingest_log = Table(
    "ingest_log", METADATA,
    Column("source", String, primary_key=True),    # ex: rte:generation_per_unit | synop
    Column("chunk_key", String, primary_key=True),  # ex: 2015-01-01_2015-01-08 | 201501
    Column("rows", Integer),
    Column("status", String),
    Column("updated_at", String),
)


def get_engine(db_url: str) -> Engine:
    engine = create_engine(db_url, future=True)
    # Réglages SQLite pour des écritures par lots plus rapides et fiables.
    with engine.begin() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA synchronous=NORMAL"))
    return engine


def init_db(engine: Engine) -> None:
    """Crée les tables fixes si absentes."""
    METADATA.create_all(engine)


# ----- Tables RTE génériques (créées à la demande) -------------------------

def ensure_rte_table(engine: Engine, table_name: str) -> None:
    """Crée une table de série temporelle RTE au schéma long standard si absente."""
    ddl = f"""
    CREATE TABLE IF NOT EXISTS "{table_name}" (
        ts_utc      TEXT NOT NULL,
        ts_end_utc  TEXT,
        series_key  TEXT NOT NULL DEFAULT '',
        sub_key     TEXT NOT NULL DEFAULT '',
        label       TEXT NOT NULL DEFAULT '',
        value       REAL,
        PRIMARY KEY (ts_utc, series_key, sub_key)
    )
    """
    with engine.begin() as conn:
        conn.execute(text(ddl))


# ----- Upsert générique -----------------------------------------------------

def upsert_df(
    engine: Engine,
    table_name: str,
    df: pd.DataFrame,
    pk_cols: Iterable[str],
) -> int:
    """Insère/remplace les lignes de ``df`` dans ``table_name`` (INSERT OR REPLACE).

    La table doit déjà exister. Retourne le nombre de lignes écrites.
    """
    if df is None or df.empty:
        return 0
    cols = list(df.columns)
    placeholders = ", ".join(f":{c}" for c in cols)
    collist = ", ".join(f'"{c}"' for c in cols)
    sql = text(
        f'INSERT OR REPLACE INTO "{table_name}" ({collist}) VALUES ({placeholders})'
    )
    # NaN -> None pour SQLite
    records = df.astype(object).where(pd.notna(df), None).to_dict("records")
    with engine.begin() as conn:
        conn.execute(sql, records)
    return len(records)


def write_table_replace(
    engine: Engine, table_name: str, df: pd.DataFrame, chunksize: int | None = None
) -> int:
    """Recrée intégralement une table à partir d'un DataFrame (utilisé pour la table maître)."""
    df.to_sql(table_name, engine, if_exists="replace", index=False, chunksize=chunksize)
    return len(df)


def log_ingest(
    engine: Engine, source: str, chunk_key: str, rows: int, status: str = "ok"
) -> None:
    from datetime import datetime

    upsert_df(
        engine,
        "ingest_log",
        pd.DataFrame(
            [{
                "source": source,
                "chunk_key": chunk_key,
                "rows": rows,
                "status": status,
                "updated_at": datetime.now(UTC).isoformat(),
            }]
        ),
        ["source", "chunk_key"],
    )


def already_ingested(engine: Engine, source: str, chunk_key: str) -> bool:
    sql = text(
        "SELECT 1 FROM ingest_log WHERE source=:s AND chunk_key=:c AND status='ok' LIMIT 1"
    )
    with engine.connect() as conn:
        return conn.execute(sql, {"s": source, "c": chunk_key}).first() is not None


@contextmanager
def connect(engine: Engine) -> Iterator:
    with engine.connect() as conn:
        yield conn
