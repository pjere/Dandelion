"""Re-fit on the ERA5-extended (ARCO, 47-yr) record, simulate, and validate — end to end.
Prints the validation score so we can see the effect of the refinements + record extension.
"""
from __future__ import annotations

import re
import time
import warnings
from pathlib import Path

import xarray as xr

warnings.filterwarnings("ignore")
from weathergen.cli import fit_model
from weathergen.config import load_config
from weathergen.simulate import simulate

from weathergen import io, validate

cfg = load_config(Path(__file__).resolve().parents[1] / "config.yaml")

t0 = time.time()
model = fit_model(cfg)                       # build_dataset now fuses the 47-yr ERA5 record
model.save(cfg.models_dir / "fitted.json")
print(f"[refit] fitted on extended record in {time.time()-t0:.0f}s (EOF modes {model.dependence.n_modes})", flush=True)

sim = simulate(model, cfg, cfg.rng())
(cfg.models_dir.parent / "output").mkdir(parents=True, exist_ok=True)
xr.Dataset({"obs": sim}).to_netcdf(cfg.models_dir.parent / "output" / "simulation.nc")
print(f"[refit] simulated {sim.sizes['time']//8760} yr", flush=True)

# validate against the STATION-only observed record (truth, not the extension)
obs, _ = io.load_station_cube(cfg, cfg.rng())
obs, _, _ = io.qc(obs, cfg)
path = validate.validate(obs, sim, cfg)
html = path.read_text(encoding="utf-8")
print("[refit] " + re.search(r"Weighted score: \d+/\d+", html).group(0), flush=True)
for m in re.finditer(r"<tr><td>(.*?)</td><td>(.*?)</td><td><span class=.(pass|warnf).>(PASS|WARN)</span>", html):
    print(f"  [{m.group(4):4s}] {m.group(1):30s} {m.group(2)}", flush=True)
print("[refit] DONE", flush=True)
