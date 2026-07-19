"""Phase 2 (finalisation) — fit the station→ERA5-100 m wind transfers on the historical overlap.

Onshore: national mean station-10 m wind → national mean ERA5-100 m wind (the national onshore fleet
is driven by the national-aggregate hub-height wind). Offshore: each farm's nearest coastal station
10 m → that farm's ERA5-100 m grid point (the coastal→offshore correlation model). Persisted to
models/wind_transfers.json and used by the conversion/calibration layers. Includes the §2.A cross-check:
is the station→100 m transfer materially worse than ERA5-100 m itself as a predictor?
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from powersim_core.serialize import load_params, save_params

from ..config import Config
from ..io.era5 import read_era5_point
from .wind import WindTransfer, fit_wind_transfer, transfer_quality_vs_era5


def _wt_to_dict(m: WindTransfer) -> dict:
    return {"region": m.region, "lags": list(m.lags), "coef": m.coef,
            "resid_std": m.resid_std, "r2": m.r2}


def _wt_from_dict(x: dict) -> WindTransfer:
    return WindTransfer(region=x["region"], lags=tuple(x["lags"]), coef=x["coef"],
                        resid_std=x["resid_std"], r2=x["r2"])


def load_wind_transfers(path: str | Path) -> dict:
    """Reload the wind-transfer bundle from its portable JSON (mirror of the write in fit_wind_transfers)."""
    d = load_params(Path(path).with_suffix(".json"))
    return {"onshore": _wt_from_dict(d["onshore"]),
            "offshore": {f: {"transfer": _wt_from_dict(v["transfer"]),
                             "nearest_station": v["nearest_station"]} for f, v in d["offshore"].items()},
            "crosscheck": d["crosscheck"]}


def _era5_100m(config: Config, point_id: str) -> pd.Series | None:
    """100 m wind for one ERA5 point, read from the DB (era5_point_hourly)."""
    return read_era5_point(config, point_id, "wind100_ms")


def _nearest_station(farm_lat, farm_lon, stations: pd.DataFrame) -> str:
    d = (stations["latitude"] - farm_lat) ** 2 + (stations["longitude"] - farm_lon) ** 2
    return str(stations.loc[d.idxmin(), "station_id"])


def fit_wind_transfers(config: Config) -> dict:
    """Fit + persist the onshore + offshore wind transfers; return a fit-quality report."""
    from ..io.loaders import load_weather_hist
    weather, stations = load_weather_hist(config)
    w10_nat = weather.groupby("timestamp_utc")["wind_speed_ms"].mean()

    sids = [str(s) for s in stations["station_id"]]
    cols = [s for s in (_era5_100m(config, sid) for sid in sids) if s is not None]
    era5_100_nat = pd.concat(cols, axis=1).mean(axis=1)

    onshore = fit_wind_transfer(w10_nat, era5_100_nat, region="FR_onshore")

    # §2.A cross-check: transfer-predicted 100 m vs raw ERA5 100 m as onshore-CF predictors
    from ..io.loaders import capacity_factor
    from ..io.qc import qc_capacity_factor
    cf = qc_capacity_factor(config, capacity_factor(config))
    on_cf = (cf[(cf.technology == "wind_onshore") & cf.is_valid]
             .set_index("timestamp_utc")["cf"])
    pred100 = onshore.predict(w10_nat)
    j = pd.concat([on_cf.rename("cf"), pred100.rename("pred"), era5_100_nat.rename("era5")],
                  axis=1).dropna()
    r2_station = float(np.corrcoef(j["cf"], j["pred"])[0, 1] ** 2)
    r2_era5 = float(np.corrcoef(j["cf"], j["era5"])[0, 1] ** 2)
    crosscheck = transfer_quality_vs_era5(r2_station, r2_era5)

    # offshore per farm: nearest coastal station 10 m → farm ERA5-100 m
    offshore = {}
    wb = config.resolve(config.section("assumptions")["workbook"])
    if wb and Path(wb).exists():
        from powersim_core.scenario import load_sheet
        farms = load_sheet(wb, "res", "offshore_farms")
        st_w = {sid: weather[weather.station_id.astype(str) == sid]
                .set_index("timestamp_utc")["wind_speed_ms"] for sid in sids}
        for _, fr in farms.iterrows():
            e100 = _era5_100m(config, f"farm_{fr['farm']}")
            if e100 is None:
                continue
            nsid = _nearest_station(fr["latitude"], fr["longitude"], stations)
            m = fit_wind_transfer(st_w[nsid], e100, region=f"offshore_{fr['farm']}")
            offshore[str(fr["farm"])] = {"transfer": m, "nearest_station": nsid}

    payload = {"onshore": _wt_to_dict(onshore),
               "offshore": {f: {"transfer": _wt_to_dict(v["transfer"]),
                                "nearest_station": v["nearest_station"]} for f, v in offshore.items()},
               "crosscheck": {"r2_station": r2_station, "r2_era5": r2_era5, "verdict": crosscheck}}
    out = config.models_dir / "wind_transfers.json"          # portable JSON (no pickle — REVIEW F6)
    save_params(payload, out)

    report = {"onshore_r2": round(onshore.r2, 3), "onshore_b": round(float(onshore.coef[1]), 3),
              "n_offshore": len(offshore), "r2_station": round(r2_station, 3),
              "r2_era5": round(r2_era5, 3), "crosscheck": crosscheck, "saved": str(out)}
    return report
