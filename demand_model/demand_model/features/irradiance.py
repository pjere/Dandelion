"""Phase 2 — national irradiance from cloud cover (§5, and the step-(iv) solar input).

Clear-sky GHI (deterministic from sun position, pvlib Haurwitz — no external turbidity data) is
modulated by cloud cover via the Kasten–Czeplak relation  GHI = GHI_cs · (1 − 0.75·CF^3.4). Because
cloud cover is available in BOTH the historical data and the weather generator, the *same* function
drives calibration and projection (and later the solar-generation model), guaranteeing consistency.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def clearsky_ghi(times_utc: pd.DatetimeIndex, stations: pd.DataFrame) -> pd.DataFrame:
    """Clear-sky GHI (W/m²) per station on ``times_utc`` (tz-aware). Columns = station_id."""
    from pvlib.location import Location
    out = {}
    for _, r in stations.iterrows():
        loc = Location(float(r["latitude"]), float(r["longitude"]),
                       altitude=float(r.get("altitude", 0) or 0), tz="UTC")
        out[str(r["station_id"])] = loc.get_clearsky(times_utc, model="haurwitz")["ghi"].to_numpy()
    return pd.DataFrame(out, index=times_utc)


def ghi_from_cloud(clearsky: pd.DataFrame, cloud_pct: pd.DataFrame) -> pd.DataFrame:
    """Kasten–Czeplak cloud attenuation of clear-sky GHI."""
    cf = (cloud_pct.reindex_like(clearsky) / 100.0).clip(0, 1)
    return (clearsky * (1.0 - 0.75 * cf.pow(3.4))).clip(lower=0.0)


def national_ghi(weather: pd.DataFrame, stations: pd.DataFrame, weights: pd.Series) -> pd.Series:
    """Weighted national GHI (W/m²) from station cloud cover + clear-sky."""
    times = pd.DatetimeIndex(pd.unique(weather["timestamp_utc"])).sort_values()
    cs = clearsky_ghi(times, stations)
    cloud = weather.pivot_table(index="timestamp_utc", columns="station_id",
                                values="cloud_cover_pct", aggfunc="mean").reindex(index=times, columns=cs.columns)
    ghi = ghi_from_cloud(cs, cloud)
    w = weights.reindex(ghi.columns).fillna(0.0).to_numpy()
    x = ghi.to_numpy()
    mask = ~np.isnan(x)
    wsum = (mask * w).sum(axis=1)
    num = np.nansum(np.where(mask, x * w, 0.0), axis=1)
    return pd.Series(np.where(wsum > 0, num / np.where(wsum > 0, wsum, 1), 0.0),
                     index=times, name="GHI_nat")
