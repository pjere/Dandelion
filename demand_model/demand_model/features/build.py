"""Phase 2 — assemble the national feature/design matrix for calibration and projection.

Collapses the per-station weather to national aggregates (T_nat + smoothed variants, HDD/CDD, GHI)
and joins the calendar/day-type design + anomaly flags + a linear trend. Cached to Parquet so the
heavy per-station weather is processed once.
"""
from __future__ import annotations

import pandas as pd

from powersim_core import lake

from ..config import Config
from .calendar import build_calendar
from .irradiance import national_ghi
from .temperature import heating_cooling, national_temperature, smoothed_temperatures, station_weights

# default threshold guesses; calibration (Phase 3) re-estimates the knees from the data
_TAU_HEAT, _TAU_COOL = 15.0, 20.0


def build_features(config: Config, weather: pd.DataFrame, stations: pd.DataFrame) -> pd.DataFrame:
    ec = config.section("effective_temperature")
    weights = station_weights(config, stations["station_id"].astype(str).tolist())

    t_nat = national_temperature(weather, weights)
    smooth = smoothed_temperatures(t_nat, ec["smoothing_halflives_h"], ec["lagged_daily_means"])
    t_slow = smooth[f"T_smooth_{ec['smoothing_halflives_h'][-1]}h"]
    hc = heating_cooling(t_slow, _TAU_HEAT, _TAU_COOL)
    ghi = national_ghi(weather, stations, weights)
    cal = build_calendar(t_nat.index)

    feat = pd.concat([smooth, hc, ghi, cal], axis=1)
    feat["trend_years"] = (feat.index - feat.index[0]).total_seconds() / (365.25 * 24 * 3600)
    # anomaly regressors (COVID / sobriety) so the trend isn't poisoned
    for name, win in config.section("data").get("anomaly_windows", {}).items():
        feat[f"is_{name}"] = ((feat.index >= pd.Timestamp(win["start"], tz="UTC")) &
                              (feat.index <= pd.Timestamp(win["end"], tz="UTC"))).astype(float)
    feat.index.name = "timestamp_utc"
    return feat


def national_features(config: Config, force: bool = False) -> pd.DataFrame:
    """Load weather -> national feature frame, cached to models/national_features.parquet."""
    if lake.exists("demand", "national_features") and not force:
        return lake.read_table("demand", "national_features")
    from ..io.loaders import load_weather
    weather, stations = load_weather(config)
    feat = build_features(config, weather, stations)
    lake.write_table(feat, "demand", "national_features")
    return feat
