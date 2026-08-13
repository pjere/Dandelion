"""`io.unclassified_gen`: the four techs mapped by `PSR2TECH` but claimed by neither dispatch list.

The properties pinned here are the ones whose failure would be silent and expensive: an inexact split
leaks or invents energy, and a mis-fired solar test would credit industrial baseload to the RES trajectory
in every projected year.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from dispatch_model.io import unclassified_gen as uc


def _series(days: int, night: float, amplitude: float, seed: int = 0) -> pd.Series:
    """Hourly series = a `night` baseload + a `amplitude` half-sine daylight bump (0 at night)."""
    idx = pd.date_range("2024-03-01", periods=24 * days, freq="h", tz="UTC")
    rng = np.random.default_rng(seed)
    h = idx.hour.to_numpy()
    solar = np.where((h >= 6) & (h <= 18), np.sin(np.pi * (h - 6) / 12.0), 0.0) * amplitude
    base = night * (1.0 + 0.05 * rng.standard_normal(len(idx)))
    return pd.Series(base + solar, index=idx)


def test_split_is_exact_and_non_negative():
    """mustrun + variable == other in EVERY hour. A split that leaks energy would silently move net load."""
    s = _series(30, night=2000.0, amplitude=9000.0)
    mustrun, variable, _ = uc._split_other(s)
    assert np.allclose((mustrun + variable).to_numpy(), s.to_numpy(), atol=1e-9)
    assert (mustrun >= -1e-9).all() and (variable >= -1e-9).all()
    assert (mustrun <= s + 1e-9).all(), "the floor may never net off more than was metered"


def test_solar_shaped_series_is_detected_and_flat_one_is_not():
    ratio_solar = uc._split_other(_series(30, night=2000.0, amplitude=9000.0))[2]
    ratio_flat = uc._split_other(_series(30, night=2000.0, amplitude=0.0))[2]
    assert ratio_solar >= uc._SOLAR_DAY_NIGHT, "a daylight bump must read as solar"
    assert ratio_flat < uc._SOLAR_DAY_NIGHT, "a flat baseload must not"
    # measured separation on 2024: NL 3.04, runner-up (GB) 1.39 — the threshold sits in a wide gap
    assert 1.39 < uc._SOLAR_DAY_NIGHT < 3.04


def test_floor_tracks_the_night_not_its_low_percentile():
    """The regression that a per-month p10 floor caused: baseload leaking into the solar bucket at night.

    With a 2 GW baseload and a daylight-only bump, the variable part must be ~0 in the dead of night. The
    p10 form left 0.9 GW there and inflated Dutch PV to 26.3 TWh against IRENA's ~21.
    """
    s = _series(60, night=2000.0, amplitude=9000.0, seed=3)
    _, variable, _ = uc._split_other(s)
    night = variable[(variable.index.hour >= 23) | (variable.index.hour <= 3)]
    assert night.mean() < 0.06 * 2000.0, f"baseload leaking into the solar part: {night.mean():.1f} MW"


def test_floor_survives_metering_dropouts():
    """A dropout day must not hand its whole baseload to the solar bucket — hence the rolling median."""
    s = _series(30, night=2000.0, amplitude=9000.0, seed=5)
    s.iloc[24 * 10:24 * 10 + 6] = 30.0                      # a night of near-zero metering
    mustrun, variable, _ = uc._split_other(s)
    other_nights = variable[(variable.index.hour >= 23) | (variable.index.hour <= 3)]
    assert other_nights.mean() < 0.10 * 2000.0
    assert np.allclose((mustrun + variable).to_numpy(), s.to_numpy(), atol=1e-9)


def test_flag_is_read_at_call_time_not_import_time(monkeypatch):
    monkeypatch.setenv("DISPATCH_UNCLASSIFIED_GEN", "0")
    assert uc.enabled() is False
    monkeypatch.setenv("DISPATCH_UNCLASSIFIED_GEN", "1")
    assert uc.enabled() is True
    monkeypatch.delenv("DISPATCH_UNCLASSIFIED_GEN", raising=False)
    monkeypatch.delenv("DISPATCH_UNCLASSIFIED_MUSTRUN", raising=False)
    monkeypatch.delenv("DISPATCH_UNCLASSIFIED_SOLAR", raising=False)
    # the shipped configuration: solar half ON, must-run half OFF — the arm the gate measured best
    # (|mean err| 11.00 / log_err 0.692 vs the baseline's 11.15 / 0.737).
    assert uc.enabled() is True and uc.solar_enabled() is True
    assert uc.mustrun_enabled() is False, (
        "the must-run half is OFF by default: real generation, but it costs 1.7 EUR/MWh of pooled mean "
        "error against a model already biased cheap in five of eight zones. See the module docstring.")


@pytest.mark.parametrize("zone", ["GB"])
def test_gb_is_untouched(zone, monkeypatch):
    """`gb_embedded` nets a load-vs-generation RESIDUAL, which already absorbs these techs."""
    idx = pd.date_range("2024-01-01", periods=48, freq="h", tz="UTC")
    df = pd.DataFrame({"load_mw": 30000.0, "musttake_res_mw": 5000.0}, index=idx)
    monkeypatch.setattr(uc, "components", lambda *a, **k: pytest.fail("GB must not be touched"))
    out = uc.apply_to_netload(object(), zone, 2024, df)
    assert out is df


def test_mustrun_leaves_load_and_solar_stays_curtailable(monkeypatch):
    """Must-run off load, solar onto must-take — and net load falls by exactly their sum either way.

    Net load is invariant to this choice, so what the assertion actually protects is CURTAILABILITY.
    Netting the solar off load instead makes it inflexible negative demand: measured on 2024 that ran
    NL's surplus hours past the RES bid floor to the -500 EUR/MWh price floor in 997 hours, and took its
    pooled mean error from -19.8 to -60.6.
    """
    idx = pd.date_range("2024-01-01", periods=24, freq="h", tz="UTC")
    df = pd.DataFrame({"load_mw": 12000.0, "musttake_res_mw": 2000.0}, index=idx)
    comp = pd.DataFrame({"mustrun_mw": 1500.0, "res_mw": 2500.0}, index=idx)
    monkeypatch.setattr(uc, "components", lambda *a, **k: comp)
    monkeypatch.setattr("dispatch_model.neighbours.blocks.constituents", lambda z: [z])
    monkeypatch.setenv("DISPATCH_UNCLASSIFIED_GEN", "1")
    monkeypatch.setenv("DISPATCH_UNCLASSIFIED_MUSTRUN", "1")   # this test exercises the must-run half
    out = uc.apply_to_netload(object(), "NL", 2024, df)
    assert (out["load_mw"] == 12000.0 - 1500.0).all()
    assert (out["musttake_res_mw"] == 2000.0 + 2500.0).all(), "solar must stay curtailable"
    before = df["load_mw"] - df["musttake_res_mw"]
    after = out["load_mw"] - out["musttake_res_mw"]
    assert np.allclose((before - after).to_numpy(), 1500.0 + 2500.0)
    assert (out["unclassified_mustrun_mw"] == 1500.0).all()
    assert (out["unclassified_res_mw"] == 2500.0).all()


def test_btm_solar_is_suppressed_only_when_the_solar_half_is_on():
    """Disabling the solar half must RESTORE `btm_solar`, not leave NL with no solar at all.

    The two call sites gate on `enabled() and solar_enabled()`. If either were gated on `enabled()`
    alone, `DISPATCH_UNCLASSIFIED_SOLAR=0` would remove the metered series AND keep the synthetic one
    suppressed, silently deleting ~22 TWh of Dutch PV.
    """
    import inspect
    from dispatch_model.rolling import backtest, projection
    for mod in (backtest, projection):
        src = inspect.getsource(mod)
        assert "_unclassified_on() and _uc_solar()" in src, (
            f"{mod.__name__}: btm_solar must be gated on the SOLAR half, not the master flag")


def test_apply_is_a_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("DISPATCH_UNCLASSIFIED_GEN", "0")
    idx = pd.date_range("2024-01-01", periods=24, freq="h", tz="UTC")
    df = pd.DataFrame({"load_mw": 12000.0, "musttake_res_mw": 2000.0}, index=idx)
    assert uc.apply_to_netload(object(), "NL", 2024, df) is df


def test_load_is_floored_at_zero(monkeypatch):
    """Same guard as `gb_embedded`: this subtracts measured supply, so a negative demand would be an
    estimator artefact rather than a real export."""
    idx = pd.date_range("2024-01-01", periods=24, freq="h", tz="UTC")
    df = pd.DataFrame({"load_mw": 400.0, "musttake_res_mw": 0.0}, index=idx)
    comp = pd.DataFrame({"mustrun_mw": 900.0, "res_mw": 0.0}, index=idx)
    monkeypatch.setattr(uc, "components", lambda *a, **k: comp)
    monkeypatch.setattr("dispatch_model.neighbours.blocks.constituents", lambda z: [z])
    monkeypatch.setenv("DISPATCH_UNCLASSIFIED_GEN", "1")
    monkeypatch.setenv("DISPATCH_UNCLASSIFIED_MUSTRUN", "1")   # this test exercises the must-run half
    out = uc.apply_to_netload(object(), "NL", 2024, df)
    assert (out["load_mw"] == 0.0).all()


def test_the_four_techs_are_exactly_those_no_dispatch_list_claims():
    """Pins the premise. If someone adds `other` to `_MUSTTAKE`, this module double-counts — fail loudly."""
    from dispatch_model.io.entsoe_hist import PSR2TECH
    from dispatch_model.neighbours.blocks import _DISPATCHABLE, _MUSTTAKE
    claimed = set(_DISPATCHABLE) | set(_MUSTTAKE)
    orphans = {t for t in PSR2TECH.values() if t not in claimed}
    assert orphans == set(uc.UNCLASSIFIED), f"dispatch-class coverage changed: {orphans}"
