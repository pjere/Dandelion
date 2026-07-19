"""Fit the 100 m-wind co-generation model (conditioned on 10 m station wind).

Assembles historical (SYNOP 10 m from the pricemodeling DB, ERA5 100 m from the step-(iv) extract)
panels for the simulated cube's stations, fits weathergen.wind100.Wind100Model, saves it to
weathergen/models/wind100.json. Run once; simulate() then co-generates 100 m wind on every draw.
    python scripts/fit_wind100.py
"""
from __future__ import annotations

import sqlite3
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from weathergen.wind100 import fit_wind100  # noqa: E402

DB = ROOT.parent / "data" / "pricemodeling.db"
ERA5 = ROOT.parent / "res_model" / "era5_cache"
CUBE = ROOT / "output" / "simulation.nc"
PERIOD = ("2015-01-01", "2026-06-30")


def _stations() -> list[str]:
    ds = xr.open_dataset(CUBE); st = [str(s) for s in ds["obs"]["station"].values]; ds.close()
    return st


def _panel_master(stations: list[str], suffix: str, clip0: bool) -> pd.DataFrame:
    """Wide station panel for `meteo_<station>_<suffix>` from master_hourly (index = ts_utc)."""
    con = sqlite3.connect(DB)
    cols = {r[1] for r in con.execute('PRAGMA table_info("master_hourly")').fetchall()}
    sel = [f'meteo_{s}_{suffix}' for s in stations if f'meteo_{s}_{suffix}' in cols]
    df = pd.read_sql(f'SELECT ts_utc, {", ".join(sel)} FROM master_hourly '
                     f"WHERE ts_utc >= ? AND ts_utc <= ?", con, params=PERIOD)
    con.close()
    df.index = pd.to_datetime(df["ts_utc"], utc=True)
    out = df.drop(columns=["ts_utc"])
    if clip0:
        out = out.clip(lower=0)
    out.columns = [c[len("meteo_"):-len(f"_{suffix}")] for c in out.columns]   # meteo_<station>_<suffix> → station
    return out


def _panel_10m(stations: list[str]) -> pd.DataFrame:
    return _panel_master(stations, "wind_speed_ms", clip0=True)


def _panel_temp(stations: list[str]) -> pd.DataFrame:
    return _panel_master(stations, "temperature_c", clip0=False)


def _panel_100m(stations: list[str]) -> pd.DataFrame:
    cols = {}
    for s in stations:
        f = ERA5 / f"era5_{s}.zip"
        if not f.exists():
            continue
        dest = f.parent / (f.stem + "_x"); dest.mkdir(exist_ok=True)
        with zipfile.ZipFile(f) as z:
            z.extractall(dest)
        ds = xr.open_dataset(str(sorted(dest.glob("*.nc"))[0]))
        if "valid_time" in ds.coords and "time" not in ds.coords:
            ds = ds.rename({"valid_time": "time"})
        t = pd.to_datetime(ds["time"].values, utc=True)
        w100 = np.sqrt(np.asarray(ds["u100"].values, float) ** 2 + np.asarray(ds["v100"].values, float) ** 2)
        cols[s] = pd.Series(w100.ravel(), index=t)
        ds.close()
    return pd.DataFrame(cols)


def main() -> None:
    stations = _stations()
    print(f"[fit_wind100] {len(stations)} stations", flush=True)
    # temp_panel is DELIBERATELY not passed → transfer-only model. The temperature-conditioning capability
    # exists in fit_wind100 (#79) but is OFF: measured on the historical panels, the co-generated 100 m wind
    # already reproduces the *deseasonalized within-winter* temp coupling (cube 0.24 vs hist 0.28), and a
    # local station-level temp term makes it WORSE (see weathergen/WIND_TEMP_COUPLING.md). Set `USE_TEMP=True`
    # only to reproduce that (rejected) experiment.
    USE_TEMP = False
    w10, w100 = _panel_10m(stations), _panel_100m(stations)
    common = w10.index.intersection(w100.index)
    w10, w100 = w10.loc[common], w100.loc[common]
    keep = [s for s in stations if s in w10.columns and s in w100.columns]
    temp = _panel_temp(stations).reindex(common) if USE_TEMP else None
    print(f"[fit_wind100] {len(keep)} stations aligned, {len(common)} hours "
          f"(temp conditioning: {'ON' if USE_TEMP else 'OFF'})", flush=True)
    model = fit_wind100(w10[keep], w100[keep], temp_panel=temp[keep] if temp is not None else None)
    out = model.save(ROOT / "models" / "wind100.json")
    print(f"[fit_wind100] R² {model.fit_r2.mean():.3f} (min {model.fit_r2.min():.3f}) | "
          f"b {model.b.mean():.3f} | φ {model.phi.mean():.3f} | σ {model.sigma.mean():.3f}", flush=True)
    print(f"[fit_wind100] saved -> {out}", flush=True)


if __name__ == "__main__":
    main()
