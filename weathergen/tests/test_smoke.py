"""End-to-end smoke test: the whole fit -> simulate -> validate pipeline on tiny
synthetic data, in well under a minute. Proves the wiring before real statistics.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from weathergen.cli import fit_model
from weathergen.config import Config, load_config
from weathergen.model import FittedModel
from weathergen.simulate import simulate
from weathergen.validate import validate

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"


def _smoke_config(tmp_path: Path) -> Config:
    cfg = load_config(CONFIG_PATH)
    cfg.raw["data"]["source"] = "synthetic"
    cfg.raw["data"]["era5"]["enabled"] = False        # smoke stays offline (no CDS)
    cfg.raw["run"]["models_dir"] = str(tmp_path / "models")
    cfg.raw["run"]["reports_dir"] = str(tmp_path / "reports")
    cfg.raw["simulate"]["horizon_years"] = 2          # keep tiny
    cfg.raw["simulate"]["start_year"] = 2027
    return cfg


def test_smoke_end_to_end(tmp_path):
    cfg = _smoke_config(tmp_path)

    # fit + serialize round-trip
    model = fit_model(cfg)
    path = model.save(cfg.models_dir / "fitted.json")
    model2 = FittedModel.load(path)
    assert model2.dependence.n_modes >= 1
    assert set(model2.var_names) == set(cfg.var_names)

    # simulate >= 2 years hourly, all modeled vars present (+ derived humidity), no NaN, in bounds
    sim = simulate(model2, cfg, cfg.rng())
    sim_vars = set(map(str, sim["variable"].values))
    assert sim.sizes["time"] == 2 * 8760
    assert set(cfg.var_names) <= sim_vars
    assert "humidity_pct" in sim_vars                  # derived from temp + dew point (D3.2)
    assert not np.isnan(sim.values).any(), "simulation must not contain NaN"
    for v in cfg.var_names:
        lo, hi = cfg.variables[v]["bounds"]
        vals = sim.sel(variable=v).values
        assert vals.min() >= lo - 1e-6 and vals.max() <= hi + 1e-6

    # reproducibility: same seed + config => identical output
    sim_again = simulate(model2, cfg, cfg.rng())
    np.testing.assert_array_equal(sim.values, sim_again.values)

    # validation report builds against the synthetic "observed" cube and carries
    # the mandatory tail-uncertainty warning
    from weathergen import io
    obs = io.build_dataset(cfg, cfg.rng()).station_cube
    report = validate(obs, sim, cfg)
    assert report.exists()
    assert "TAIL-UNCERTAINTY" in report.read_text(encoding="utf-8")
