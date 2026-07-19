"""Data contracts (pandera) for dispatch inputs/outputs. UTC timestamps; fail loudly."""
from __future__ import annotations

from pandera.pandas import Check, Column, DataFrameSchema

from powersim_core.schemas import validate  # noqa: F401  (re-exported: single contract-check authority)

# --- LP outputs --------------------------------------------------------------
ZONAL_PRICES = DataFrameSchema(
    {
        "timestamp_utc": Column("datetime64[ns, UTC]", nullable=False),
        "zone": Column(str, nullable=False),
        "price_eur_mwh": Column(float, nullable=False),          # dual of the zonal balance
    },
    strict=False, coerce=True,
)

DISPATCH = DataFrameSchema(
    {
        "timestamp_utc": Column("datetime64[ns, UTC]", nullable=False),
        "zone": Column(str, nullable=False),
        "unit_or_block": Column(str, nullable=False),
        "tech": Column(str, nullable=False),
        "output_mw": Column(float, Check.ge(-1e-6), nullable=False),
    },
    strict=False, coerce=True,
)

FLOWS = DataFrameSchema(
    {
        "timestamp_utc": Column("datetime64[ns, UTC]", nullable=False),
        "from_zone": Column(str, nullable=False),
        "to_zone": Column(str, nullable=False),
        "flow_mw": Column(float, nullable=False),
    },
    strict=False, coerce=True,
)

# --- historical (backtest reference) -----------------------------------------
HIST_PRICES = DataFrameSchema(
    {
        "timestamp_utc": Column("datetime64[ns, UTC]", nullable=False),
        "zone": Column(str, nullable=False),
        "price_eur_mwh": Column(float, nullable=False),
    },
    strict=False, coerce=True,
)


