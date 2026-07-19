"""Phase 2 — GHI processing (D3). Irradiance is derived from cloud cover with the SAME chain as the
demand model (step iii): clear-sky GHI (pvlib Haurwitz — no turbidity dependency) × Kasten–Czeplak
cloud attenuation ``GHI = GHI_cs·(1 − 0.75·CF³·⁴)``. Using the identical relation guarantees the
demand↔PV irradiance signal is one and the same, so their correlation is preserved by construction.

If step (ii) ever emits GHI directly (or ERA5 SSRD is folded in), swap the source here — the PV chain
downstream is agnostic to how ``ghi_wm2`` was produced.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

_KC_K, _KC_P = 0.75, 3.4          # Kasten–Czeplak attenuation constants (shared with step iii)


def clearsky_ghi(times: pd.DatetimeIndex, lat: float, lon: float, alt: float = 0.0) -> pd.Series:
    """Haurwitz clear-sky GHI (W/m²) at a point for tz-aware UTC ``times``."""
    import pvlib
    loc = pvlib.location.Location(lat, lon, tz="UTC", altitude=alt)
    cs = loc.get_clearsky(times, model="haurwitz")
    return cs["ghi"].astype(float)


def ghi_from_cloud(times: pd.DatetimeIndex, lat: float, lon: float, cloud_pct, alt: float = 0.0
                   ) -> pd.Series:
    """Cloud cover (%) → GHI (W/m²) via clear-sky × Kasten–Czeplak attenuation."""
    cf = np.clip(np.asarray(cloud_pct, float) / 100.0, 0.0, 1.0)
    ghi_cs = clearsky_ghi(times, lat, lon, alt).to_numpy()
    ghi = ghi_cs * (1.0 - _KC_K * cf ** _KC_P)
    return pd.Series(np.clip(ghi, 0.0, None), index=times, name="ghi_wm2")


def station_ghi(weather: pd.DataFrame, stations: pd.DataFrame) -> pd.DataFrame:
    """Add a ``ghi_wm2`` column to the tidy weather frame, per station, from its cloud cover."""
    meta = stations.set_index(stations["station_id"].astype(str))
    out = []
    for sid, g in weather.groupby(weather["station_id"].astype(str)):
        g = g.sort_values("timestamp_utc")
        row = meta.loc[sid]
        alt = float(row["altitude"]) if "altitude" in row and pd.notna(row["altitude"]) else 0.0
        ts = pd.DatetimeIndex(g["timestamp_utc"])
        g = g.copy()
        g["ghi_wm2"] = ghi_from_cloud(ts, float(row["latitude"]), float(row["longitude"]),
                                      g["cloud_cover_pct"].to_numpy(), alt).to_numpy()
        out.append(g)
    return pd.concat(out, ignore_index=True)


def national_ghi(weather: pd.DataFrame, stations: pd.DataFrame,
                 weights: dict[str, float] | None = None) -> pd.Series:
    """Fleet-weighted national GHI series (W/m²). Equal weights if none given."""
    g = station_ghi(weather, stations)
    ids = [str(s) for s in stations["station_id"]]
    w = weights or {s: 1.0 / len(ids) for s in ids}
    g["w"] = g["station_id"].astype(str).map(w).fillna(0.0)
    num = (g["ghi_wm2"] * g["w"]).groupby(g["timestamp_utc"]).sum()
    den = g["w"].groupby(g["timestamp_utc"]).sum()
    return (num / den).rename("GHI_nat")
