"""Phase 0 — data contracts (§3). All timestamps tz-aware UTC; one weather interface for historical
and synthetic. Schemas fail loudly on violations (same standard as step iii)."""
from __future__ import annotations

from pandera.pandas import Check, Column, DataFrameSchema

from powersim_core.schemas import validate  # noqa: F401  (re-exported: single contract-check authority)

# A. Historical production (capacity-normalised downstream) -------------------
#    tech ∈ {pv, wind_onshore, wind_offshore, hydro_ror}; region "FR" until regional extraction.
PRODUCTION_HIST = DataFrameSchema(
    {
        "timestamp_utc": Column("datetime64[ns, UTC]", nullable=False),
        "technology": Column(str, nullable=False),
        "region": Column(str, nullable=False),
        "production_mw": Column(float, Check.ge(0), nullable=True),
        "capacity_mw": Column(float, Check.gt(0), nullable=True, required=False),
    },
    strict=False, coerce=True,
)

# Capacity factor series after normalisation + QC ----------------------------
CF_HIST = DataFrameSchema(
    {
        "timestamp_utc": Column("datetime64[ns, UTC]", nullable=False),
        "technology": Column(str, nullable=False),
        "region": Column(str, nullable=False),
        "cf": Column(float, Check.in_range(0.0, 1.05), nullable=True),
        "is_valid": Column(bool, nullable=False),          # False on outage/ramp-up/curtailment flags
    },
    strict=False, coerce=True,
)

# B. Registry — plant metadata ----------------------------------------------
REGISTRY = DataFrameSchema(
    {
        "unit_id": Column(str, nullable=False),
        "technology": Column(str, nullable=False),
        "region": Column(str, nullable=True),
        "latitude": Column(float, Check.in_range(-90, 90), nullable=True, required=False),
        "longitude": Column(float, Check.in_range(-180, 180), nullable=True, required=False),
        "commissioning_year": Column("Int64", nullable=True, required=False),
        "capacity_mw": Column(float, Check.gt(0), nullable=True, required=False),
    },
    strict=False, coerce=True,
)

# C. Weather — tidy per-station hourly (historical OR synthetic draw) ---------
WEATHER = DataFrameSchema(
    {
        "timestamp_utc": Column("datetime64[ns, UTC]", nullable=False),
        "station_id": Column(str, nullable=False),
        "temperature_c": Column(float, Check.in_range(-40, 55), nullable=True),
        "wind_speed_ms": Column(float, Check.in_range(0, 75), nullable=True),
        "ghi_wm2": Column(float, Check.in_range(0, 1400), nullable=True, required=False),
        "cloud_cover_pct": Column(float, Check.in_range(0, 100), nullable=True, required=False),
        "precip_1h_mm": Column(float, Check.in_range(0, 200), nullable=True, required=False),
    },
    strict=False, coerce=True,
)

# D. Assumptions workbook — one tidy sheet family per §4 ----------------------
CAPACITY_TRAJECTORIES = DataFrameSchema(
    {
        "technology": Column(str, nullable=False),
        "region": Column(str, nullable=False),
        "year": Column(int, Check.in_range(1990, 2100), nullable=False),
        "capacity_mw": Column(float, Check.ge(0), nullable=False),
        "scenario": Column(str, nullable=False, required=False),
    },
    strict=False, coerce=True,
)

TECHNOLOGY_VINTAGES = DataFrameSchema(
    {
        "technology": Column(str, nullable=False),
        "cohort_year": Column(int, Check.in_range(1990, 2100), nullable=False),
        "variable": Column(str, nullable=False),
        "value": Column(float, nullable=False),
        "scenario": Column(str, nullable=False, required=False),
    },
    strict=False, coerce=True,
)

# generic tidy sheet (degradation_availability / spatial_distribution / losses) with a year axis
TIDY_YEAR_SHEET = DataFrameSchema(
    {
        "technology": Column(str, nullable=False, required=False),
        "region": Column(str, nullable=False, required=False),
        "year": Column(int, Check.in_range(1990, 2100), nullable=False, required=False),
        "variable": Column(str, nullable=False),
        "unit": Column(str, nullable=False, required=False),
        "value": Column(float, nullable=False),
        "scenario": Column(str, nullable=False, required=False),
    },
    strict=False, coerce=True,
)

OFFSHORE_FARMS = DataFrameSchema(
    {
        "farm": Column(str, nullable=False),
        "latitude": Column(float, Check.in_range(41, 52), nullable=False),
        "longitude": Column(float, Check.in_range(-6, 10), nullable=False),
        "capacity_mw": Column(float, Check.gt(0), nullable=False),
        "commissioning_year": Column(int, Check.in_range(2020, 2100), nullable=False),
        "foundation": Column(str, nullable=False),          # fixed | floating
        "turbine_class": Column(str, nullable=True, required=False),
        "scenario": Column(str, nullable=False, required=False),
    },
    strict=False, coerce=True,
)


