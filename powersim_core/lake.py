"""The Parquet lake — the single storage authority for model outputs (§6, ADR-4).

One physical layout for every artifact::

    data/lake/{layer}/{dataset}/[key=value/...]/part.parquet   (zstd)

`layer` is the producing step (availability / demand / res / dispatch / weathergen), `dataset` the
artifact, and the `key=value` directories are Hive-style partitions (scenario / realization / year).
All writes go through `write_table`, all reads through `read_table`; nothing else touches output paths.
DuckDB views over these globs are built by `powersim_core.catalog`.

Design notes:
- **Row order is preserved** (no implicit sort) — downstream fingerprints/joins depend on it; pass
  `sort_by` explicitly if you want ordered storage.
- `index=` mirrors `DataFrame.to_parquet` (write the index or not) so migrated writers reproduce their
  exact prior on-disk shape.
- `schema=` (a pandera `DataFrameSchema`) validates on write — the data contract is enforced at the
  boundary, loudly, before anything persists.
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd


def repo_root() -> Path:
    return Path(os.environ.get("POWERSIM_ROOT") or Path(__file__).resolve().parents[1])


def lake_root() -> Path:
    """Lake root, resolved per call so `POWERSIM_LAKE` (e.g. a test tmp dir) always takes effect."""
    return Path(os.environ.get("POWERSIM_LAKE") or (repo_root() / "data" / "lake"))


def dataset_dir(layer: str, dataset: str) -> Path:
    return lake_root() / layer / dataset


def _part_dir(layer: str, dataset: str, partitions: dict) -> Path:
    d = dataset_dir(layer, dataset)
    for k, v in partitions.items():
        d = d / f"{k}={v}"
    return d


def table_path(layer: str, dataset: str, **partitions) -> Path:
    """Absolute path of the single `part.parquet` for one (dataset, partition) tuple."""
    return _part_dir(layer, dataset, partitions) / "part.parquet"


def dataset_glob(layer: str, dataset: str) -> str:
    """Recursive glob string over all partitions of a dataset (for DuckDB `read_parquet`)."""
    return str(dataset_dir(layer, dataset) / "**" / "part.parquet")


def write_table(df: pd.DataFrame, layer: str, dataset: str, *, index: bool = True,
                sort_by: str | list[str] | None = None, schema=None,
                compression: str = "zstd", **partitions) -> Path:
    """Validate (if `schema`), optionally sort, and write one Parquet partition. Returns the path."""
    if schema is not None:
        df = schema.validate(df, lazy=True)
    if sort_by is not None:
        df = df.sort_values(sort_by)
    p = table_path(layer, dataset, **partitions)
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(p, compression=compression, index=index)
    return p


def read_table(layer: str, dataset: str, **partitions) -> pd.DataFrame:
    """Read one partition (if partitions given) or concatenate every partition of a dataset."""
    if partitions:
        return pd.read_parquet(table_path(layer, dataset, **partitions))
    parts = sorted(dataset_dir(layer, dataset).glob("**/part.parquet"))
    if not parts:
        raise FileNotFoundError(f"no partitions for {layer}/{dataset} under {dataset_dir(layer, dataset)}")
    if len(parts) == 1:
        return pd.read_parquet(parts[0])
    return pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)


def exists(layer: str, dataset: str, **partitions) -> bool:
    if partitions:
        return table_path(layer, dataset, **partitions).exists()
    d = dataset_dir(layer, dataset)
    return d.exists() and any(d.glob("**/part.parquet"))
