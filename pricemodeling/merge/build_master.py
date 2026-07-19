"""Construction de la table maître horaire (master_hourly) + détail par groupe (fact_hourly_long).

Principe
--------
* Grille horaire continue en **UTC** sur la période (pas de trou, pas d'ambiguïté DST).
* Chaque source brute (schéma long) est ré-échantillonnée à l'heure puis pivotée en colonnes :
    - puissances / prix / flux  -> moyenne horaire ;
    - stock hydraulique / capacités (pas large) -> report (ffill).
* Colonnes météo = moyenne France des stations SYNOP (3 h -> interpolée à l'heure).
* ``ts_local`` (Europe/Paris) et ``utc_offset_h`` ajoutés pour l'usage métier.
* Le détail production **par groupe** va dans fact_hourly_long (haute cardinalité), nommé
  via le référentiel canonique dim_production_unit.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date
from itertools import islice

import pandas as pd
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def _bulk_write_sqlite(engine: Engine, table: str, df: pd.DataFrame, batch: int = 5000) -> int:
    """Écrit une (très large) table via sqlite3 natif : bien plus rapide que to_sql.

    PRAGMA synchronous=OFF + commits par lots (progrès persistés). Colonnes object -> TEXT,
    sinon REAL. Les NaN sont stockés par SQLite comme NULL (= donnée manquante)."""
    db_path = engine.url.database
    cols = list(df.columns)
    coldefs = ", ".join(f'"{c}" {"TEXT" if df[c].dtype == object else "REAL"}' for c in cols)
    placeholders = ", ".join("?" * len(cols))
    engine.dispose()  # libère les connexions SQLAlchemy (évite "database is locked")
    con = sqlite3.connect(db_path, timeout=120)
    try:
        con.execute("PRAGMA synchronous=OFF")
        con.execute(f'DROP TABLE IF EXISTS "{table}"')
        con.execute(f'CREATE TABLE "{table}" ({coldefs})')
        insert = f'INSERT INTO "{table}" VALUES ({placeholders})'
        rows = df.itertuples(index=False, name=None)
        while True:
            chunk = list(islice(rows, batch))
            if not chunk:
                break
            con.executemany(insert, chunk)
            con.commit()
    finally:
        con.close()
    return len(df)

# (table source, préfixe de colonne, méthode d'agrégation horaire)
MASTER_SOURCES = [
    ("rte_generation_per_type", "prod", "mean"),
    ("rte_consumption_short_term", "conso", "mean"),
    ("rte_market_prices", "price", "mean"),
    ("rte_generation_forecast", "fc", "mean"),
    ("rte_physical_flows", "flow", "mean"),
    ("rte_water_reserves", "hydro_stock", "ffill"),
    ("rte_installed_capacities_per_type", "capacity", "ffill"),
    ("entsoe_day_ahead_prices", "price_da", "mean"),
]


def _slug(value: str) -> str:
    s = re.sub(r"[^0-9A-Za-z]+", "_", str(value).strip()).strip("_").lower()
    return s or "na"


def _table_exists(engine: Engine, name: str) -> bool:
    return name in inspect(engine).get_table_names()


def _hourly_grid(start: date, end: date) -> pd.DatetimeIndex:
    end_excl = pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)
    return pd.date_range(pd.Timestamp(start, tz="UTC"), end_excl, freq="1h", inclusive="left")


def _read_long(engine: Engine, table: str) -> pd.DataFrame:
    with engine.connect() as conn:
        df = pd.read_sql(text(f'SELECT ts_utc, series_key, sub_key, value FROM "{table}"'), conn)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True, errors="coerce")
    df = df[df["ts"].notna()]
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    key = df["series_key"].astype(str)
    has_sub = df["sub_key"].astype(str).str.len() > 0
    df["key"] = key.where(~has_sub, key + "_" + df["sub_key"].astype(str))
    return df


def _pivot_hourly(df: pd.DataFrame, prefix: str, agg: str, grid: pd.DatetimeIndex) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(index=grid)
    df = df.copy()
    df["hour"] = df["ts"].dt.floor("h")
    grouped = df.groupby(["hour", "key"])["value"]
    series = grouped.mean() if agg == "mean" else grouped.last()
    wide = series.unstack("key")
    wide = wide.reindex(grid)
    if agg == "ffill":
        wide = wide.ffill()
    wide.columns = [f"{prefix}_{_slug(c)}" for c in wide.columns]
    return wide


def _meteo_france_mean(engine: Engine, grid: pd.DatetimeIndex) -> pd.DataFrame:
    if not _table_exists(engine, "synop_obs"):
        return pd.DataFrame(index=grid)
    # Moyenne France calculée en SQL (évite de charger ~28 M lignes dans pandas) :
    # une valeur par (horodatage 3 h, variable).
    with engine.connect() as conn:
        df = pd.read_sql(
            text(
                "SELECT ts_utc, variable, AVG(value) AS value "
                "FROM synop_obs GROUP BY ts_utc, variable"
            ),
            conn,
        )
    if df.empty:
        return pd.DataFrame(index=grid)
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True, errors="coerce")
    df = df[df["ts"].notna()]
    avg = df.pivot_table(index="ts", columns="variable", values="value", aggfunc="mean")
    # 3 h -> horaire : réindex sur l'union, interpolation temporelle bornée, puis grille
    full = avg.reindex(avg.index.union(grid)).sort_index()
    full = full.interpolate(method="time", limit=3)
    out = full.reindex(grid)
    out.columns = [f"meteo_{_slug(c)}_fr" for c in out.columns]
    return out


def _meteo_stations_block(engine: Engine, grid: pd.DatetimeIndex) -> pd.DataFrame:
    """Bloc large météo PAR STATION : une colonne par couple (station, paramètre).

    Construit station par station pour limiter la mémoire (chaque sous-bloc ne fait que
    ~nb_paramètres colonnes), puis concatène. Obs 3 h interpolées à l'heure (limite 3 h), float32.
    Retourne un DataFrame indexé par ``grid`` (sans colonnes temporelles).
    """
    if not _table_exists(engine, "synop_obs"):
        return pd.DataFrame(index=grid)
    with engine.connect() as conn:
        stations = [r[0] for r in conn.execute(
            text("SELECT DISTINCT station_id FROM synop_obs ORDER BY station_id")
        ).fetchall()]

    blocks: list[pd.DataFrame] = []
    for sid in stations:
        with engine.connect() as conn:
            df = pd.read_sql(
                text("SELECT ts_utc, variable, value FROM synop_obs WHERE station_id = :s"),
                conn, params={"s": sid},
            )
        if df.empty:
            continue
        df["ts"] = pd.to_datetime(df["ts_utc"], utc=True, errors="coerce")
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        df = df[df["ts"].notna()]
        wide = df.pivot_table(index="ts", columns="variable", values="value", aggfunc="mean")
        full = wide.reindex(wide.index.union(grid)).sort_index().interpolate(method="time", limit=3)
        out = full.reindex(grid).astype("float32")
        out.columns = [f"meteo_{sid}_{_slug(c)}" for c in out.columns]
        blocks.append(out)

    if not blocks:
        return pd.DataFrame(index=grid)
    return pd.concat(blocks, axis=1)


def _unit_prod_block(engine: Engine, grid: pd.DatetimeIndex) -> pd.DataFrame:
    """Bloc large production PAR GROUPE : une colonne ``unit_<groupe>`` par groupe canonique,
    pivoté depuis fact_hourly_long. Retourne un DataFrame indexé par ``grid`` (float32)."""
    if not _table_exists(engine, "fact_hourly_long"):
        return pd.DataFrame(index=grid)
    with engine.connect() as conn:
        df = pd.read_sql(text("SELECT ts_utc, variable, value FROM fact_hourly_long"), conn)
    if df.empty:
        return pd.DataFrame(index=grid)
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True, errors="coerce")
    df = df[df["ts"].notna()]
    wide = df.pivot_table(index="ts", columns="variable", values="value", aggfunc="mean")
    out = wide.reindex(grid).astype("float32")
    out.columns = [f"unit_{_slug(c)}" for c in out.columns]
    return out


def _build_unit_detail(engine: Engine) -> int:
    """Production horaire par groupe (canonique) -> fact_hourly_long, via agrégation SQL.

    Évite de charger les ~14 M lignes de rte_generation_per_unit dans pandas : le rattachement
    EIC -> nom canonique (avec fusion éventuelle d'EIC) et l'agrégation horaire se font en SQL.
    Les horodatages per_unit sont déjà horaires ; on les normalise par troncature.
    """
    if not _table_exists(engine, "rte_generation_per_unit"):
        return 0
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS fact_hourly_long"))
        conn.execute(text(
            """
            CREATE TABLE fact_hourly_long (
                ts_utc   TEXT NOT NULL,
                source   TEXT NOT NULL,
                variable TEXT NOT NULL,
                value    REAL,
                PRIMARY KEY (ts_utc, source, variable)
            )
            """
        ))
        # d1 : eic observé -> canonical_eic ; d2 : canonical_eic -> nom canonique
        conn.execute(text(
            """
            INSERT INTO fact_hourly_long (ts_utc, source, variable, value)
            SELECT substr(u.ts_utc, 1, 13) || ':00:00+00:00' AS h,
                   'rte_generation_per_unit' AS source,
                   COALESCE(d2.canonical_name, d1.canonical_name, u.series_key) AS variable,
                   SUM(u.value) AS value
            FROM rte_generation_per_unit u
            LEFT JOIN dim_production_unit d1 ON u.series_key = d1.eic_code
            LEFT JOIN dim_production_unit d2 ON d1.canonical_eic = d2.eic_code
            WHERE u.value IS NOT NULL
            GROUP BY h, variable
            """
        ))
        n = conn.execute(text("SELECT COUNT(*) FROM fact_hourly_long")).scalar()
    return int(n or 0)


def build_master(
    engine: Engine,
    start: date,
    end: date,
    timezone_local: str = "Europe/Paris",
    include_units: bool = True,
    include_stations: bool = True,
    force_units: bool = False,
) -> dict:
    """Construit l'unique table large ``master_hourly`` (1 ligne = 1 heure UTC) :
    agrégats nationaux + météo France + **météo par station** + **production par groupe**.

    Construit aussi/maintient la table longue ``fact_hourly_long`` (détail par groupe) si
    ``include_units`` (c'est elle qui alimente le bloc ``unit_*`` du master).
    """
    grid = _hourly_grid(start, end)
    master = pd.DataFrame(index=grid)
    master.index.name = "ts_utc"

    # 1) Agrégats nationaux RTE (schéma long -> colonnes)
    for table, prefix, agg in MASTER_SOURCES:
        if not _table_exists(engine, table):
            continue
        master = master.join(_pivot_hourly(_read_long(engine, table), prefix, agg, grid))

    # 2) Météo moyenne France
    master = master.join(_meteo_france_mean(engine, grid))

    # 3) Météo détaillée par station (~ stations × paramètres colonnes)
    if include_stations:
        master = master.join(_meteo_stations_block(engine, grid))

    # 4) Production par groupe (colonnes unit_*) — d'abord (re)construire fact_hourly_long
    detail_rows = 0
    if include_units:
        # fact_hourly_long est coûteuse (14 M lignes) : on ne la reconstruit que si nécessaire
        existing = 0
        if _table_exists(engine, "fact_hourly_long"):
            with engine.connect() as conn:
                existing = conn.execute(text("SELECT COUNT(*) FROM fact_hourly_long")).scalar() or 0
        detail_rows = existing if (existing and not force_units) else _build_unit_detail(engine)
        master = master.join(_unit_prod_block(engine, grid))

    # 5) Colonnes temporelles en tête
    local = grid.tz_convert(timezone_local)
    offset_h = [int(t.utcoffset().total_seconds() // 3600) for t in local]
    master.insert(0, "ts_local", local.strftime("%Y-%m-%dT%H:%M:%S%z"))
    master.insert(1, "utc_offset_h", offset_h)

    out = master.reset_index()
    out["ts_utc"] = out["ts_utc"].dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    rows = _bulk_write_sqlite(engine, "master_hourly", out)

    # la table large par station autonome est désormais redondante avec master_hourly
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS meteo_stations_hourly"))

    return {
        "master_rows": rows,
        "master_cols": out.shape[1],
        "fact_long_rows": detail_rows,
        "period": f"{start} -> {end}",
    }
