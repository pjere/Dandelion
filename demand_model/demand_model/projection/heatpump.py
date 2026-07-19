"""Heat-pump COP(T) for the demand projection's heating driver (#40).

Replaces the single cold-COP derate *scalar* (0.62) with a physically-grounded COP–temperature curve whose
steepness depends on the fleet's rated seasonal COP (SCOP) — so the derate **improves as the fleet
modernises** rather than staying fixed.

Scope / no double-counting. The heating component's hourly temperature SHAPE (MW/°C) is already in the
calibrated statistical model. This module supplies only the projection's fleet-evolution knob: how the
growing, modernising HP fleet turns its rated SCOP into the cold-weather electricity it actually draws on the
winter heating gradient. Evaluated at a single cold reference (−7 °C, the winter-gradient design condition)
it is an annual factor, exactly matching the intent of the scalar it replaces; the full curve — including the
electric-resistance backup kink below the balance point — is exposed for validation and any hourly use.

Physics (air-source HP, literature 2026):
  * COP ≈ SCOP·[1 + k·(T − T_ref)], with k ≈ 2–3 %/°C of the rated COP (e.g. 4.0 @ +8 °C → ~2.0 @ −8 °C;
    +35 % COP from +7 → +20 °C; ScienceDirect S0306261917308954, arXiv 2503.07213).
  * Better (inverter / EVI / cold-climate) units have BOTH a higher SCOP and a *gentler* slope k, so the
    fleet's cold derate rises with SCOP. k(SCOP) is calibrated so a SCOP-2.8 fleet reproduces the historical
    0.62 derate at −7 °C (preserving the demand calibration) and flattens toward 0.019 /°C for SCOP ≥ 4.5.
  * Below a balance point (~−10 °C) electric-resistance backup engages (COP → 1), steepening the cold tail
    (GB field trials, ScienceDirect S0360544226018438).
"""
from __future__ import annotations

import numpy as np

T_REF_SCOP = 7.0          # °C — temperature at which the rated seasonal COP (SCOP) applies
T_COLD_GRADIENT = -7.0    # °C — winter heating-gradient cold reference (design condition)
_SLOPE_AT_SCOP28 = 0.0271     # /°C → 1 − 14·0.0271 = 0.62 derate at −7 °C for a SCOP-2.8 (legacy) fleet
_SLOPE_FLATTEN = 0.0048       # /°C per unit of SCOP — better units hold COP better in the cold
_SLOPE_MIN, _SLOPE_MAX = 0.018, 0.032


def cop_slope(scop):
    """COP–temperature slope (fraction of rated COP lost per °C) for a fleet rated at seasonal `scop`.
    Steep for cheap/legacy units, flatter for high-SCOP inverter/cold-climate units."""
    return np.clip(_SLOPE_AT_SCOP28 - _SLOPE_FLATTEN * (np.asarray(scop, float) - 2.8), _SLOPE_MIN, _SLOPE_MAX)


def cop_at_temperature(scop, temp_c, *, balance_point_c=-10.0, backup_span_c=10.0, max_backup_frac=0.6):
    """Effective HP + resistance-backup COP at outdoor `temp_c` for a fleet rated at `scop`.

    Linear COP(T) decline (slope from ``cop_slope``, floored at COP 1); below ``balance_point_c`` a growing
    share ``f`` of the heat is met by electric-resistance backup (COP 1), and the effective COP for the total
    heat is the heat-weighted harmonic mean 1 / ((1−f)/cop_hp + f/1)."""
    scop = np.asarray(scop, float)
    T = np.asarray(temp_c, float)
    cop_hp = np.maximum(scop * (1.0 + cop_slope(scop) * (T - T_REF_SCOP)), 1.0)
    f = np.clip((balance_point_c - T) / backup_span_c, 0.0, 1.0) * max_backup_frac
    return 1.0 / ((1.0 - f) / cop_hp + f)


def cold_derate(scop, cold_ref_c: float = T_COLD_GRADIENT, **kw):
    """Cold-weather COP as a fraction of rated SCOP — the projection heating-gradient derate that replaces
    the 0.62 scalar. ≈0.62 for a SCOP-2.8 fleet, rising toward ~0.73 as SCOP improves to ~4.5."""
    scop = np.asarray(scop, float)
    return cop_at_temperature(scop, cold_ref_c, **kw) / scop
