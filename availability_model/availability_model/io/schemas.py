"""Phase 0 — data contracts (§4, §5, §6.5). UTC timestamps; fail loudly on violations."""
from __future__ import annotations

from pandera.pandas import Check, Column, DataFrameSchema

from powersim_core.schemas import validate  # noqa: F401  (re-exported: single contract-check authority)

OUTAGE_STATES = ["available", "planned", "forced", "common_mode", "derated"]
OUTAGE_TYPES = ["ASR", "VP", "VD", "forced", "maintenance"]      # inferred outage classes

# --- Inferred historical outage events (from per-unit production) -------------
OUTAGE_EVENTS = DataFrameSchema(
    {
        "unit_id": Column(str, nullable=False),
        "start": Column("datetime64[ns, UTC]", nullable=False),
        "end": Column("datetime64[ns, UTC]", nullable=False),
        "duration_days": Column(float, Check.gt(0), nullable=False),
        "outage_type": Column(str, Check.isin(OUTAGE_TYPES), nullable=False),
        "capacity_mw": Column(float, Check.gt(0), nullable=True, required=False),
    },
    strict=False, coerce=True,
)

# --- Hourly available-capacity output (§6.5) ---------------------------------
AVAILABILITY = DataFrameSchema(
    {
        "unit_id": Column(str, nullable=False),
        "timestamp_utc": Column("datetime64[ns, UTC]", nullable=False),
        "available_mw": Column(float, Check.ge(0), nullable=False),
        "state": Column(str, Check.isin(OUTAGE_STATES), nullable=False),
    },
    strict=False, coerce=True,
)

# --- Fleet registry (workbook §5.1) ------------------------------------------
FLEET_REGISTRY = DataFrameSchema(
    {
        "unit_id": Column(str, nullable=False),
        "name": Column(str, nullable=True, required=False),
        "technology": Column(str, nullable=False),          # nuclear/ccgt/ocgt/coal/oil/biomass/hydro_*
        "palier": Column(str, nullable=True, required=False),   # CP0/CPY/P4/P'4/N4/EPR (nuclear)
        "capacity_mw": Column(float, Check.gt(0), nullable=False),
        "cooling": Column(str, nullable=True, required=False),  # river/sea/tower/none
        "basin": Column(str, nullable=True, required=False),    # Rhône/Loire/Garonne/…
        "commissioning_year": Column("Int64", nullable=True, required=False),
        "closure_year": Column("Int64", nullable=True, required=False),
        "scenario": Column(str, nullable=False, required=False),
    },
    strict=False, coerce=True,
)

# --- Generic tidy parameter sheet (planned/forced/common_mode/derating/hydro) -
PARAM_SHEET = DataFrameSchema(
    {
        "key": Column(str, nullable=False),                 # technology / palier / basin / border
        "variable": Column(str, nullable=False),
        "value": Column(float, nullable=True),
        "text": Column(str, nullable=True, required=False),
        "scenario": Column(str, nullable=False, required=False),
    },
    strict=False, coerce=True,
)

INTERCONNECTORS = DataFrameSchema(
    {
        "border": Column(str, nullable=False),
        "direction": Column(str, nullable=False),           # import / export
        "ntc_mw": Column(float, Check.ge(0), nullable=False),
        "planned_unavail": Column(float, Check.in_range(0, 1), nullable=True, required=False),
        "forced_unavail": Column(float, Check.in_range(0, 1), nullable=True, required=False),
        "scenario": Column(str, nullable=False, required=False),
    },
    strict=False, coerce=True,
)
