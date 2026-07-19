"""Heat-pump COP(T) curve (#40): physics, literature anchors, fleet-modernisation, calibration preservation."""
from __future__ import annotations

import numpy as np
from demand_model.projection.heatpump import (
    T_COLD_GRADIENT,
    cold_derate,
    cop_at_temperature,
    cop_slope,
)


def test_cop_falls_with_temperature_at_2_to_3_pct_per_degree():
    scop = 3.5
    # relative COP loss per °C, measured above the balance point (no backup) — should sit in 2–3 %/°C
    c10, c0 = cop_at_temperature(scop, 10.0), cop_at_temperature(scop, 0.0)
    rel_per_deg = (c10 - c0) / c10 / 10.0
    assert 0.02 <= rel_per_deg <= 0.03


def test_legacy_fleet_reproduces_the_historical_062_derate_at_minus7():
    # the whole point: a SCOP-2.8 fleet at −7 °C must still give ~0.62, so the demand calibration is preserved
    assert abs(float(cold_derate(2.8)) - 0.62) < 0.01


def test_modern_high_scop_fleet_holds_cop_better():
    legacy, modern = float(cold_derate(2.8)), float(cold_derate(4.5))
    assert modern > legacy                     # better units → higher cold derate
    assert 0.70 < modern < 0.80                # ~0.73 expected
    assert cop_slope(4.5) < cop_slope(2.8)     # and a gentler COP(T) slope


def test_literature_anchor_legacy_cop_roughly_halves_over_16_degrees():
    # task literature: a legacy air-source unit roughly halves COP from mild to cold over ~16 °C. That is a
    # LEGACY (steep) fleet — SCOP≈2.8 here — not a high-SCOP one (the "4.0" in the source is a rated-point
    # COP, not a seasonal average). Check the ratio, decoupled from the point-vs-seasonal level confusion.
    warm, cold = cop_at_temperature(2.8, 8.0), cop_at_temperature(2.8, -8.0)
    assert 0.50 <= cold / warm <= 0.65


def test_resistance_backup_kink_steepens_the_cold_electricity_tail():
    scop = 3.0
    # the kink shows up in ELECTRICITY intensity (1/COP) — the quantity that drives demand — rising faster
    # below the balance point than above it (COP itself flattens toward 1, so measure the reciprocal).
    def elec(t):
        return 1.0 / cop_at_temperature(scop, t)
    above = elec(-8.0) - elec(-6.0)
    below = elec(-16.0) - elec(-14.0)
    assert below > above > 0
    assert cop_at_temperature(scop, -14.0) > 1.0     # HP still contributes at moderate cold
    # in extreme cold everything is resistance: effective COP collapses to exactly 1 (never below)
    assert cop_at_temperature(scop, -30.0) == 1.0


def test_cold_ref_is_minus7_and_derate_is_monotonic_in_scop():
    assert T_COLD_GRADIENT == -7.0
    scops = np.linspace(2.5, 5.0, 20)
    d = cold_derate(scops)
    assert np.all(np.diff(d) >= 0)             # never decreases as fleet improves
    assert np.all((d > 0.5) & (d < 0.85))
