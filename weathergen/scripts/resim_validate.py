import re
import warnings
from pathlib import Path

import xarray as xr

warnings.filterwarnings("ignore")
from weathergen.config import load_config
from weathergen.model import FittedModel
from weathergen.simulate import simulate

from weathergen import io, validate

cfg = load_config(Path(__file__).resolve().parents[1]/"config.yaml")
model = FittedModel.load(cfg.models_dir/"fitted.json")
sim = simulate(model, cfg, cfg.rng())
xr.Dataset({"obs": sim}).to_netcdf(cfg.models_dir.parent/"output"/"simulation.nc")
obs,_ = io.load_station_cube(cfg, cfg.rng()); obs,_,_ = io.qc(obs, cfg)
html = validate.validate(obs, sim, cfg).read_text(encoding="utf-8")
print(re.search(r"Weighted score: \d+/\d+", html).group(0), flush=True)
for m in re.finditer(r"<tr><td>(.*?)</td><td>(.*?)</td><td><span class=.(pass|warnf).>(PASS|WARN)</span>", html):
    print(f"  [{m.group(4):4s}] {m.group(1):28s} {m.group(2)}", flush=True)
print("RESIM DONE", flush=True)
