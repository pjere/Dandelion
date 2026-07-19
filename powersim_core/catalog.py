"""DuckDB catalog over the Parquet lake (§6, ADR-4).

`build_catalog()` scans `data/lake/{layer}/{dataset}/…/part.parquet` and (re)creates one DuckDB **view**
per dataset in `data/powersim.duckdb`, with Hive partition columns (scenario / realization / year)
projected out. Models and analysts then query one catalog — `SELECT * FROM demand__projection_hourly` —
instead of chasing file paths. A `_catalog` table summarises every dataset; an append-only `runs` ledger
records write events for provenance.

    from powersim_core import catalog
    catalog.build_catalog()                       # refresh views after a run
    df = catalog.query("SELECT * FROM dispatch__backtest_prices WHERE year = 2019")
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from . import lake


def db_path_default() -> Path:
    return Path(os.environ.get("POWERSIM_DUCKDB") or (lake.lake_root().parent / "powersim.duckdb"))


def view_name(layer: str, dataset: str) -> str:
    return f"{layer}__{dataset}"


def discover_datasets() -> list[tuple[str, str]]:
    """Every (layer, dataset) under the lake root that holds at least one `part.parquet`."""
    out = []
    if not lake.lake_root().exists():
        return out
    for layer_dir in sorted(p for p in lake.lake_root().iterdir() if p.is_dir()):
        for ds_dir in sorted(p for p in layer_dir.iterdir() if p.is_dir()):
            if any(ds_dir.glob("**/part.parquet")):
                out.append((layer_dir.name, ds_dir.name))
    return out


def connect(db_path: str | Path | None = None):
    import duckdb
    return duckdb.connect(str(db_path or db_path_default()))


def build_catalog(db_path: str | Path | None = None) -> Path:
    """(Re)build the DuckDB catalog: one view per dataset + a `_catalog` summary. Returns the db path."""
    db_path = Path(db_path or db_path_default())
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = connect(db_path)
    try:
        rows = []
        for layer, dataset in discover_datasets():
            glob = lake.dataset_glob(layer, dataset).replace("\\", "/").replace("'", "''")
            view = view_name(layer, dataset)
            con.execute(                                    # DuckDB can't prepare DDL → inline the glob
                f'CREATE OR REPLACE VIEW "{view}" AS '
                f"SELECT * FROM read_parquet('{glob}', hive_partitioning=true, union_by_name=true)")
            n = con.execute(f'SELECT count(*) FROM "{view}"').fetchone()[0]
            rows.append((layer, dataset, view, int(n)))
        con.execute("CREATE OR REPLACE TABLE _catalog "
                    "(layer VARCHAR, dataset VARCHAR, view_name VARCHAR, n_rows BIGINT)")
        if rows:
            con.executemany("INSERT INTO _catalog VALUES (?, ?, ?, ?)", rows)
        con.execute("CREATE TABLE IF NOT EXISTS runs "
                    "(ts_utc VARCHAR, layer VARCHAR, dataset VARCHAR, partitions VARCHAR, "
                    "path VARCHAR, n_rows BIGINT, git_hash VARCHAR)")
    finally:
        con.close()
    return db_path


def record_run(layer: str, dataset: str, path: str | Path, n_rows: int,
               partitions: dict | None = None, git_hash: str = "", db_path=None) -> None:
    """Append a write event to the provenance ledger (best-effort; models may call after a write)."""
    con = connect(db_path)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS runs "
                    "(ts_utc VARCHAR, layer VARCHAR, dataset VARCHAR, partitions VARCHAR, "
                    "path VARCHAR, n_rows BIGINT, git_hash VARCHAR)")
        con.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [datetime.now(UTC).isoformat(), layer, dataset,
                     ",".join(f"{k}={v}" for k, v in (partitions or {}).items()),
                     str(path), int(n_rows), git_hash])
    finally:
        con.close()


def query(sql: str, db_path: str | Path | None = None):
    """Run a read query against the catalog and return a DataFrame."""
    con = connect(db_path)
    try:
        return con.execute(sql).df()
    finally:
        con.close()
