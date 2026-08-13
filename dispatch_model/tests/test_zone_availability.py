"""`neighbours.blocks._measured_avail` — the per-zone availability floor.

The global `_AVAIL_FACTOR["nuclear"] = 0.78` put four zones' capacity ceiling BELOW their own observed
annual mean output (BE 3065 vs 3385, ES 5551 vs 5957, CH 2317 vs 2614, NL 379 vs 385 MW on 2024). A
ceiling below the mean of what actually happened cannot be reproduced by any dispatch, so ~1.0 GW of
baseload was permanently absent.

The fix is a FLOOR — `max(default, median(observed)/nameplate)` — never a replacement. That asymmetry is
the whole safety argument: `METHODOLOGY.md` records that generation quantiles UNDER-read rarely-dispatched
plant (DE gas 11.8 GW read against 31.7 installed), and a floor cannot do that because it only raises.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from dispatch_model.neighbours import blocks


class _Cfg:
    """Minimal stand-in; `_measured_avail` only passes it to `load_generation_hist`."""


def _patch_gen(monkeypatch, tech: str, series: np.ndarray):
    idx = pd.date_range("2024-01-01", periods=len(series), freq="h", tz="UTC")
    df = pd.DataFrame({"timestamp_utc": idx, "tech": tech, "gen_mw": series})
    monkeypatch.setattr(blocks, "load_generation_hist", lambda *a, **k: df)


def test_floor_raises_a_derating_that_sits_below_observed_output(monkeypatch):
    """The BE nuclear case: nameplate 3929, default 0.78 → 3065, but the fleet ran at a median of ~3460."""
    _patch_gen(monkeypatch, "nuclear", np.full(8784, 3460.0))
    f = blocks._measured_avail(_Cfg(), "BE", "nuclear", 2024, 3929.0)
    assert f > 0.78, "the floor must lift a derating the data contradicts"
    assert 3929.0 * f >= 3460.0 - 1.0, "capacity must reach the observed median"


def test_floor_never_lowers_a_peaker(monkeypatch):
    """The documented failure mode this must not reproduce: DE gas read 11.8 GW against 31.7 installed."""
    rng = np.random.default_rng(0)
    _patch_gen(monkeypatch, "gas", rng.uniform(0.0, 6000.0, 8784))      # median ~0.16 of nameplate
    f = blocks._measured_avail(_Cfg(), "DE_LU", "gas", 2024, 31700.0)
    assert f == blocks._AVAIL_FACTOR["gas"], "a rarely-dispatched fleet must keep its nameplate derating"


def test_factor_is_capped_at_one(monkeypatch):
    """Metering can read slightly above declared nameplate; capacity may not exceed it."""
    _patch_gen(monkeypatch, "nuclear", np.full(8784, 4200.0))
    assert blocks._measured_avail(_Cfg(), "CH", "nuclear", 2024, 3900.0) == 1.0


def test_genuinely_derated_fleet_keeps_its_default(monkeypatch):
    """GB's AGR fleet really does run at ~0.50 of nameplate — the floor must leave it alone."""
    _patch_gen(monkeypatch, "nuclear", np.full(8784, 4600.0))
    f = blocks._measured_avail(_Cfg(), "GB", "nuclear", 2024, 9229.0)
    assert f == 0.78, f"expected the 0.78 default to stand, got {f}"


def test_flag_off_restores_the_global_constant(monkeypatch):
    monkeypatch.setenv("DISPATCH_ZONE_AVAIL", "0")
    _patch_gen(monkeypatch, "nuclear", np.full(8784, 3900.0))
    assert blocks._measured_avail(_Cfg(), "BE", "nuclear", 2024, 3929.0) == 0.78


def test_thin_or_missing_history_falls_back(monkeypatch):
    _patch_gen(monkeypatch, "nuclear", np.full(50, 3900.0))              # < 1000 hours
    assert blocks._measured_avail(_Cfg(), "BE", "nuclear", 2024, 3929.0) == 0.78
    monkeypatch.setattr(blocks, "load_generation_hist", lambda *a, **k: pd.DataFrame())
    assert blocks._measured_avail(_Cfg(), "BE", "nuclear", 2024, 3929.0) == 0.78
    assert blocks._measured_avail(_Cfg(), "BE", "nuclear", 2024, 0.0) == 0.78   # no nameplate


@pytest.mark.parametrize("tech", ["gas", "coal", "lignite", "oil", "biomass"])
def test_floor_is_monotone_and_never_below_default(monkeypatch, tech):
    """Property, not a fixture: whatever the data says, the result is in [default, 1.0]."""
    rng = np.random.default_rng(7)
    _patch_gen(monkeypatch, tech, rng.uniform(0.0, 20000.0, 8784))
    f = blocks._measured_avail(_Cfg(), "DE_LU", tech, 2024, 20000.0)
    assert blocks._AVAIL_FACTOR[tech] <= f <= 1.0
