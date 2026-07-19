"""Golden-test harness (§3): freeze the numerical fingerprint of every model's reference outputs so any
refactor can be proven result-preserving.

Fingerprints are STAT DIGESTS (per numeric column: n, sum, mean, std, min, max + a hash of values rounded
to `ROUND` dp), not raw file bytes — this captures the numbers (the thing we must preserve) and is robust
to Parquet/pyarrow version churn. The weather cube (NetCDF) is digested by per-variable global stats.

Usage:
    python tools/golden.py capture      # write golden/baseline.json from current outputs
    python tools/golden.py check        # recompute and diff vs baseline; exit 1 on any numerical change
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "golden" / "baseline.json"
ROUND = 6          # decimals at which two runs are considered numerically identical
RTOL = 1e-9        # tolerance for the scalar stats in `check`

# reference artifacts per model — now the Parquet lake (§6, ADR-4): data/lake/{layer}/{dataset}/…/part.parquet
PARQUET_ARTIFACTS = [
    "data/lake/availability/availability_by_tech/part.parquet",
    "data/lake/availability/availability_nuclear_units/part.parquet",
    "data/lake/availability/interconnectors/part.parquet",
    "data/lake/availability/reservoir_budget/part.parquet",
    "data/lake/demand/projection_features/realization=0/part.parquet",
    "data/lake/demand/projection_hourly/scenario=reference/part.parquet",
    "data/lake/res/hist_modelled_cf/part.parquet",
    "data/lake/res/proj_drivers/realization=0/part.parquet",
    "data/lake/res/production/scenario=reference/realization=0/part.parquet",
    "data/lake/dispatch/backtest_prices/year=2019/part.parquet",
]
JSON_ARTIFACTS = [
    "availability_model/reports/validation_report.json",
    "availability_model/reports/calibration_report.json",
]
CUBE = "weathergen/output/simulation.nc"


def _num_digest(s: pd.Series) -> dict:
    a = pd.to_numeric(s, errors="coerce").to_numpy(float)
    a = a[np.isfinite(a)]
    if a.size == 0:
        return {"n": 0}
    vh = hashlib.sha256(np.round(a, ROUND).tobytes()).hexdigest()[:16]
    return {"n": int(a.size), "sum": float(a.sum()), "mean": float(a.mean()),
            "std": float(a.std()), "min": float(a.min()), "max": float(a.max()), "vhash": vh}


def fingerprint_parquet(path: Path) -> dict:
    df = pd.read_parquet(path)
    cols = {}
    for c in df.columns:
        if pd.api.types.is_numeric_dtype(df[c]):
            cols[c] = _num_digest(df[c])
        else:
            cols[c] = {"n": int(df[c].notna().sum()), "nunique": int(df[c].nunique())}
    return {"shape": list(df.shape), "columns": cols}


def fingerprint_cube(path: Path) -> dict:
    import xarray as xr
    ds = xr.open_dataset(path)
    try:
        da = ds["obs"]
        out = {"dims": {k: int(v) for k, v in ds.sizes.items()}}
        for i, v in enumerate(da["variable"].values):
            arr = da.isel(variable=i).values
            arr = arr[np.isfinite(arr)]
            out[str(v)] = {"mean": float(arr.mean()), "std": float(arr.std()),
                           "min": float(arr.min()), "max": float(arr.max())}
        return out
    finally:
        ds.close()


def capture() -> dict:
    fp = {"parquet": {}, "json": {}, "cube": {}}
    for rel in PARQUET_ARTIFACTS:
        p = ROOT / rel
        if p.exists():
            fp["parquet"][rel] = fingerprint_parquet(p)
    for rel in JSON_ARTIFACTS:
        p = ROOT / rel
        if p.exists():
            fp["json"][rel] = json.loads(p.read_text())
    if (ROOT / CUBE).exists():
        fp["cube"][CUBE] = fingerprint_cube(ROOT / CUBE)
    return fp


def _diffs(base, cur, path=""):
    out = []
    if isinstance(base, dict):
        for k in base:
            if k not in cur:
                out.append(f"{path}/{k}: MISSING")
            else:
                out += _diffs(base[k], cur[k], f"{path}/{k}")
        for k in cur:
            if k not in base:
                out.append(f"{path}/{k}: NEW")
    elif isinstance(base, (int, float)) and isinstance(cur, (int, float)):
        if not np.isclose(base, cur, rtol=RTOL, atol=1e-9, equal_nan=True):
            out.append(f"{path}: {base} -> {cur}")
    elif base != cur:
        out.append(f"{path}: {base!r} -> {cur!r}")
    return out


def main(argv):
    cmd = argv[0] if argv else "check"
    if cmd == "capture":
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(capture(), indent=1, sort_keys=True))
        fp = json.loads(BASELINE.read_text())
        n = sum(len(fp[k]) for k in fp)
        print(f"[golden] baseline written -> {BASELINE} ({n} artifacts)")
    elif cmd == "check":
        if not BASELINE.exists():
            print("[golden] no baseline; run `capture` first"); return 2
        diffs = _diffs(json.loads(BASELINE.read_text()), capture())
        if diffs:
            print(f"[golden] {len(diffs)} NUMERICAL CHANGES:")
            for d in diffs[:50]:
                print("  ", d)
            return 1
        print("[golden] OK — outputs numerically identical to baseline")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
