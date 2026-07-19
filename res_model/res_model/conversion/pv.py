"""Phase 3 — solar PV physical chain (§5.1), vintage-resolved.

GHI (from cloud, D3) → Erbs decomposition → plane-of-array transposition for the cohort's
tilt/orientation/tracker mix → NOCT cell temperature + temperature derating → DC→AC clipping at the
cohort DC/AC ratio → system losses → age degradation. Output is a per-unit-AC capacity factor (0..1).
The multiplicative month×hour bias recalibration is applied in Phase 4. pvlib does all solar geometry.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

_NOCT = 45.0                       # °C, nominal operating cell temperature


@dataclass
class PVCohort:
    tilt_deg: float = 25.0
    azimuth_deg: float = 180.0     # south
    tracker_share: float = 0.0     # fraction on single-axis trackers
    dc_ac_ratio: float = 1.2
    temp_coeff_per_c: float = -0.0037
    system_loss: float = 0.14      # soiling + inverter + MV
    degradation_per_year: float = 0.005
    age_years: float = 0.0
    extra: dict = field(default_factory=dict)


def _poa_fixed(solpos, dni, ghi, dhi, dni_extra, tilt, azimuth) -> np.ndarray:
    import pvlib
    poa = pvlib.irradiance.get_total_irradiance(
        tilt, azimuth, solpos["apparent_zenith"], solpos["azimuth"],
        dni=dni, ghi=ghi, dhi=dhi, dni_extra=dni_extra, model="haydavies")
    return poa["poa_global"].to_numpy()


def _poa_tracker(solpos, dni, ghi, dhi, dni_extra) -> np.ndarray:
    import pvlib
    tr = pvlib.tracking.singleaxis(solpos["apparent_zenith"], solpos["azimuth"],
                                   axis_tilt=0, axis_azimuth=180, max_angle=55, backtrack=True)
    poa = pvlib.irradiance.get_total_irradiance(
        tr["surface_tilt"].fillna(0), tr["surface_azimuth"].fillna(180),
        solpos["apparent_zenith"], solpos["azimuth"],
        dni=dni, ghi=ghi, dhi=dhi, dni_extra=dni_extra, model="haydavies")
    return poa["poa_global"].fillna(0.0).to_numpy()


def pv_cf(times: pd.DatetimeIndex, lat: float, lon: float, ghi, temp_air,
          cohort: PVCohort, alt: float = 0.0) -> pd.Series:
    """Per-unit-AC hourly capacity factor for one PV cohort at a location."""
    import pvlib
    ghi = np.clip(np.asarray(ghi, float), 0.0, None)
    temp_air = np.asarray(temp_air, float)
    solpos = pvlib.solarposition.get_solarposition(times, lat, lon, altitude=alt)
    zen = solpos["apparent_zenith"].to_numpy()
    dni_extra = pvlib.irradiance.get_extra_radiation(times).to_numpy()
    erbs = pvlib.irradiance.erbs(pd.Series(ghi, index=times), zen, times)
    dni, dhi = erbs["dni"].to_numpy(), erbs["dhi"].to_numpy()

    poa = _poa_fixed(solpos, dni, ghi, dhi, dni_extra, cohort.tilt_deg, cohort.azimuth_deg)
    if cohort.tracker_share > 0:
        poa_tr = _poa_tracker(solpos, dni, ghi, dhi, dni_extra)
        poa = (1 - cohort.tracker_share) * poa + cohort.tracker_share * poa_tr
    poa = np.clip(np.nan_to_num(poa), 0.0, None)

    tcell = temp_air + (poa / 800.0) * (_NOCT - 20.0)                 # NOCT cell-temp model
    dc = (poa / 1000.0) * (1.0 + cohort.temp_coeff_per_c * (tcell - 25.0))   # per DC nameplate
    dc = np.clip(dc, 0.0, None)
    ac = np.minimum(dc * cohort.dc_ac_ratio, 1.0)                    # inverter clipping at AC nameplate
    ac *= (1.0 - cohort.system_loss)
    ac *= (1.0 - cohort.degradation_per_year) ** max(cohort.age_years, 0.0)
    return pd.Series(np.clip(ac, 0.0, 1.0), index=times, name="pv_cf")
