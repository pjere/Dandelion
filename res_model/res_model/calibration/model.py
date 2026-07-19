"""Phase 4 — the calibrated RES model: fitted correction parameters + apply methods.

Holds the statistical recalibration on top of the physical chains:
  pv_bias        — (month × hour) multiplicative factor so national PV matches history
  onshore/offshore — fitted power-curve params (specific power, smoothing, availability/wake scale)
  hydro          — fitted ROR baseline/sensitivity/snowmelt
Serialisable; consumed by the projection engine (Phase 6).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from powersim_core.serialize import load_params, save_params

from ..conversion.wind_offshore import offshore_farm_cf
from ..conversion.wind_onshore import onshore_cf


def _restore_int_keys(d: dict) -> dict:
    return {int(k): v for k, v in d.items()}


@dataclass
class CalibratedRes:
    pv_bias: dict[tuple[int, int], float]        # (month, hour) -> factor
    onshore: dict[str, float]                    # specific_power, smoothing_ms, cf_scale
    offshore: dict[str, float]                   # specific_power, smoothing_ms, cf_scale
    hydro: dict[str, float]                      # baseline, sensitivity, snowmelt_amp
    metrics: dict[str, Any] = field(default_factory=dict)

    # ---- apply (per-hour, given the driver already turned into a raw modelled CF or wind) ----
    def apply_pv(self, raw_pv_cf: pd.Series) -> pd.Series:
        idx = raw_pv_cf.index
        f = np.array([self.pv_bias.get((m, h), 1.0) for m, h in zip(idx.month, idx.hour)])
        return (raw_pv_cf * f).clip(0.0, 1.0).rename("pv")

    def _monthly(self, cf: pd.Series, params: dict) -> pd.Series:
        mf = params.get("monthly_factor")
        if mf:
            cf = cf * np.array([mf.get(int(m), 1.0) for m in cf.index.month])
        return cf.clip(0.0, 1.0)

    def apply_onshore(self, wind100: pd.Series) -> pd.Series:
        cf = onshore_cf(wind100, specific_power=self.onshore["specific_power"],
                        smoothing_ms=self.onshore["smoothing_ms"], availability=1.0) * self.onshore["cf_scale"]
        return self._monthly(cf, self.onshore).rename("wind_onshore")

    def apply_offshore(self, wind100: pd.Series, specific_power: float | None = None) -> pd.Series:
        cf = offshore_farm_cf(wind100, specific_power=specific_power or self.offshore["specific_power"],
                              smoothing_ms=self.offshore["smoothing_ms"], availability=1.0,
                              wake_loss=0.0) * self.offshore["cf_scale"]
        return self._monthly(cf, self.offshore).rename("wind_offshore")

    def apply_hydro(self, precip_nat: pd.Series, temp_nat: pd.Series) -> pd.Series:
        """ROR CF from the weather-driven lumped hydrological blend (see calibration/hydro.py):
        observed monthly climatology + ridge blend of multi-timescale precip, fast/slow soil-moisture
        stores and PET — all functions of precip + temperature, so projection-valid. LOYO-CV monthly
        bias ~10.6 % (vs ~15 % single-precip)."""
        from .hydro import apply_blend
        precip_nat, temp_nat = precip_nat.sort_index(), temp_nat.sort_index()
        precip_daily = precip_nat.resample("1D").sum()
        temp_daily = temp_nat.resample("1D").mean()
        cf_daily = apply_blend(self.hydro, precip_daily, temp_daily)
        cf_h = cf_daily.reindex(precip_nat.index.normalize()).to_numpy()
        return pd.Series(np.clip(cf_h, 0.02, 0.85), index=precip_nat.index, name="hydro_ror")

    def save(self, path: str | Path) -> Path:
        """Portable JSON (no pickle — REVIEW F6). `pv_bias` tuple keys → "m,h" strings; `hydro`
        month-keyed sub-dicts / `beta` array round-trip via the serializer + `load`'s key restore."""
        payload = asdict(self)
        payload["pv_bias"] = {f"{m},{h}": v for (m, h), v in self.pv_bias.items()}
        return save_params(payload, Path(path).with_suffix(".json"))

    @staticmethod
    def load(path: str | Path) -> CalibratedRes:
        d = load_params(Path(path).with_suffix(".json"))
        d["pv_bias"] = {tuple(int(x) for x in k.split(",")): v for k, v in d["pv_bias"].items()}
        # restore int month keys the hydro blend indexes by (monthly_clim[int(m)], feat_clim[c].map)
        hy = d["hydro"]
        hy["monthly_clim"] = _restore_int_keys(hy["monthly_clim"])
        hy["feat_clim"] = {c: _restore_int_keys(v) for c, v in hy["feat_clim"].items()}
        return CalibratedRes(**d)
