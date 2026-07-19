"""DISP §8 — projection-sensitivity monotone checks (commodity + availability shocks move prices right)."""
from __future__ import annotations

import pytest
from dispatch_model.config import load_config
from dispatch_model.lp.multi_zone import solve_multizone
from dispatch_model.rolling.assemble import assemble_window


@pytest.fixture(scope="module")
def scenarios():
    cfg = load_config("config.yaml")
    if not cfg.resolve(cfg.section("data")["sqlite_path"]).exists():
        pytest.skip("pricemodeling DB not present")
    if not cfg.resolve(cfg.section("data")["weathergen_output"]).exists() and False:
        pass

    def run(**kw):
        T, zd, b, ntc = assemble_window(cfg, "2019-01-14", "2019-01-21", **kw)
        p = solve_multizone(T, zd, b, ntc)["prices"]
        return p["FR"].mean(), p["DE_LU"].mean(), (p["FR"] - p["DE_LU"]).mean()

    return {"base": run(), "gas": run(price_mult={"gas": 1.5}),
            "co2": run(price_mult={"co2": 1.5}), "nuc": run(nuc_avail_mult=0.7)}


def test_gas_shock_raises_prices(scenarios):
    base, gas = scenarios["base"], scenarios["gas"]
    assert gas[0] > base[0] and gas[1] > base[1]         # +50% gas → both zones up
    assert gas[2] > base[2]                              # FR (gas-marginal) up more → spread widens


def test_co2_shock_raises_prices_more_in_coal_heavy_DE(scenarios):
    base, co2 = scenarios["base"], scenarios["co2"]
    assert co2[0] > base[0] and co2[1] > base[1]         # +50% CO2 → both up
    assert (co2[1] - base[1]) > (co2[0] - base[0])       # DE (coal/lignite) up more than FR


def test_nuclear_shock_creates_FR_premium(scenarios):
    base, nuc = scenarios["base"], scenarios["nuc"]
    assert nuc[0] > base[0]                              # FR nuclear −30% → FR price up
    assert nuc[2] > base[2]                              # FR premium vs DE widens (common-mode signature)
