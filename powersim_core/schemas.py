"""Central pandera contract vocabulary + registry (§6, ADR-4).

The single home for the data-contract primitives every package shares: the loud-failing `validate`
helper (previously copy-pasted in four `io/schemas.py` modules), canonical glossary-typed column
builders, and a name→schema `REGISTRY` so the catalog/validation layers can enumerate contracts.

Package-specific schemas still live in each `io/schemas.py` (they know their own columns), but they
build on these primitives and register themselves here via `register(name, schema)`.
"""
from __future__ import annotations

import pandera.pandas as pa
from pandera.pandas import Check, Column, DataFrameSchema


def validate(df, schema: DataFrameSchema, name: str):
    """Validate `df` against `schema`, raising a compact, actionable error on any violation."""
    try:
        return schema.validate(df, lazy=True)
    except pa.errors.SchemaErrors as exc:
        raise ValueError(f"data contract '{name}' failed:\n{exc.failure_cases.head(20)}") from exc


# --- canonical glossary-typed columns (keep dtypes/units consistent across packages) ---
def timestamp_utc_col(*, nullable: bool = False, required: bool = True) -> Column:
    return Column("datetime64[ns, UTC]", nullable=nullable, required=required)


def mw_col(*, ge0: bool = True, nullable: bool = False, required: bool = True) -> Column:
    return Column(float, [Check.ge(0)] if ge0 else None, nullable=nullable, required=required)


def fraction_col(*, nullable: bool = True, required: bool = False) -> Column:
    return Column(float, Check.in_range(0, 1), nullable=nullable, required=required)


# --- registry: name -> schema (packages register their contracts on import) ---
REGISTRY: dict[str, DataFrameSchema] = {}


def register(name: str, schema: DataFrameSchema) -> DataFrameSchema:
    REGISTRY[name] = schema
    return schema
