"""Phase 0 — data contracts (§4). All timestamps are tz-aware UTC; calendar features are
derived in Europe/Paris downstream. Schemas fail loudly on violations."""
from __future__ import annotations

from pandera.pandas import Check, Column, DataFrameSchema

from powersim_core.schemas import validate  # noqa: F401  (re-exported: single contract-check authority)

_UTC = Check(lambda s: s.dt.tz is not None, error="timestamps must be tz-aware (UTC)")

# A. Historical load (after perimeter correction: REALISED - pumping) --------
LOAD_HIST = DataFrameSchema(
    {
        "timestamp_utc": Column("datetime64[ns, UTC]", nullable=False, unique=True),
        "load_mw": Column(float, Check.in_range(0, 120000), nullable=False),
    },
    strict=False, ordered=False, coerce=True,
)

# B/C. Weather (tidy per-station hourly) — same schema for historical & synthetic ----
WEATHER = DataFrameSchema(
    {
        "timestamp_utc": Column("datetime64[ns, UTC]", nullable=False),
        "station_id": Column(str, nullable=False),
        "temperature_c": Column(float, Check.in_range(-40, 55), nullable=True),
        "wind_speed_ms": Column(float, Check.in_range(0, 75), nullable=True),
        "cloud_cover_pct": Column(float, Check.in_range(0, 100), nullable=True),
        "ghi_wm2": Column(float, Check.in_range(0, 1400), nullable=True, required=False),
        "humidity_pct": Column(float, Check.in_range(0, 100), nullable=True, required=False),
    },
    strict=False, coerce=True,
)

# E. Assumptions workbook — one tidy sheet per driver family (§6) -------------
ASSUMPTION_SHEET = DataFrameSchema(
    {
        "year": Column(int, Check.in_range(1990, 2100), nullable=False),
        "variable": Column(str, nullable=False),
        "unit": Column(str, nullable=False),
        "value": Column(float, nullable=False),
        "scenario": Column(str, nullable=False, required=False),
    },
    strict=False, coerce=True,
)

# weights sheet (station -> region -> weight) is shaped differently
WEIGHTS_SHEET = DataFrameSchema(
    {
        "station_id": Column(str, nullable=False),
        "region": Column(str, nullable=True),
        "weight": Column(float, Check.in_range(0, 1), nullable=False),
    },
    strict=False, coerce=True,
)


