"""Phase 6 — shared-weather loader: national daily temperature + annual wetness from the weathergen cube.

Reads the SAME cube demand (iii) and RES (iv) consume, so the availability side's weather coupling
(thermal derating, reservoir wetness) is on the identical draw. The current cube is a single 20-year
realization (no ensemble dim), so weather is draw-independent — the 50 projection draws vary only the
stochastic outages. The `realization` handling is kept for forward-compatibility with an ensemble cube.
"""
from __future__ import annotations

import pandas as pd

from powersim_core.weather_cube import load_national_weather as _core_load_national_weather

from ..config import Config


def load_national_weather(config: Config, realization: int = 0) -> tuple[pd.Series, dict[int, float]]:
    """→ (temp_daily national mean °C, wetness_by_year). Delegates to powersim_core (shared cube reader);
    the availability side keeps this config-based wrapper so derating/reservoir call sites are unchanged."""
    path = config.resolve(config.section("data")["weathergen_output"])
    return _core_load_national_weather(path, realization=realization)
