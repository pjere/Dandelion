"""Reactor-class **physics** registry (FLEX-F0) — time-invariant per palier/vintage, hard-coded (spec §1.3).

French PWRs load-follow in *mode G* (grey control rods + boron): they modulate within a normal operating
band down to ~60 % Pn routinely, can reach a technical minimum ~20–30 % Pn, but deep/frequent excursions
are rationed by xénon dynamics, control-rod wear and the fuel-cycle position (end-of-cycle stretch-out
removes maneuverability entirely). These are *physical* limits, so they live here, not in the workbook; the
*costs* of using them and the regulatory floors live in ``trajectories`` as year-indexed trajectories.

Sources: IAEA/NEA load-following literature (Nuclear Energy Agency, "Technical and Economic Aspects of Load
Following with Nuclear Power Plants", 2011); EDF operating practice (mode G, RGE); the numbers below are
**physics seeds** — the free calibration parameters (``c_mod``, ``c_start``, ``β``, D-caps) are fitted in
FLEX-F7 (§9). Every field is documented in the physics/regime table (``dispatch_model/FLEXIBILITY.md``).

Fields per class (all fractions are of the unit's *available* capacity, per hour where a rate):
  alpha_band    — normal operating-band floor: p ≥ α_band·u − d              (C1)
  alpha_tech    — absolute technical minimum: d ≤ (α_band − α_tech)·u        (C1)
  r_up          — normal hourly up-ramp fraction                            (C3)
  r_down        — normal hourly down-ramp fraction (generous)               (C3)
  xenon_beta    — up-ramp penalty per unit of recent deep-mod depth         (C3)
  d_max_8h      — 8-hour deep-mod energy budget, fraction of cap·h          (C2a)
  d_max_day     — daily deep-mod energy budget, fraction of cap·h           (C2b)
  rho_recommit  — min-down recommit ramp: max hourly rise of committed cap  (C5 min-down)
                  as a fraction of available cap (u_t ≤ u_{t−1} + avail·ρ). A shut reactor
                  cannot re-commit instantly (hot-standby → full-load takes hours), so ρ<1 is
                  a linear min-down-time proxy: it holds `u` down after a shutdown for ~1/ρ
                  hours, which — with the C5 start cost — makes a short negative episode cheaper
                  to ride out at the band floor than to shut and pay the recommit ramp back up.
"""
from __future__ import annotations


# palier → physics. Standard French fleet: CP0/CP1/CP2 (900 MW ×32), P4/P'4 (1300 MW ×20), N4 (1450 MW ×4),
# EPR (1650 MW, Flamanville-3), EPR2 (future, designed for enhanced flexibility). Deep-band = α_band−α_tech.
def _c(alpha_band, alpha_tech, r_up, r_down, xenon_beta, d_max_8h, d_max_day, rho_recommit) -> dict:
    return {"alpha_band": alpha_band, "alpha_tech": alpha_tech, "r_up": r_up, "r_down": r_down,
            "xenon_beta": xenon_beta, "d_max_8h": d_max_8h, "d_max_day": d_max_day,
            "rho_recommit": rho_recommit}


_CLASSES: dict[str, dict] = {
    #            α_band α_tech r_up  r_down xénon_β D8h  Dday  ρ_recommit
    "900":  _c(0.60, 0.25, 0.30, 0.50, 0.15, 2.8, 5.6, 0.20),
    "1300": _c(0.60, 0.25, 0.28, 0.48, 0.16, 2.8, 5.6, 0.18),
    "N4":   _c(0.58, 0.22, 0.26, 0.46, 0.17, 2.9, 5.8, 0.16),
    "EPR":  _c(0.55, 0.20, 0.30, 0.55, 0.14, 3.2, 6.4, 0.22),
    "EPR2": _c(0.50, 0.20, 0.35, 0.60, 0.12, 3.6, 7.2, 0.25),
}
_FALLBACK = "1300"     # unknown palier → the modal 1300 MW class (conservative middle)


def class_name(palier: str | float | int | None = None, capacity_mw: float | None = None) -> str:
    """Resolve a reactor class from an explicit `palier` label, else from unit `capacity_mw` (nearest
    standard palier). Robust to the fleet registry not carrying a palier column (maps by capacity)."""
    if palier is not None:
        p = str(palier).upper().strip()
        if p in _CLASSES:
            return p
        for key in _CLASSES:                                   # substring match ("CP1"→900 handled below)
            if key in p:
                return key
        if p.startswith(("CP", "P'", "P4", "P0", "P1", "P2")):
            return "900" if p.startswith(("CP", "P0", "P1", "P2")) else "1300"
    if capacity_mw is not None:
        c = float(capacity_mw)
        if c >= 1600:
            return "EPR"
        if c >= 1375:
            return "N4"
        if c >= 1100:
            return "1300"
        return "900"
    return _FALLBACK


def physics(palier: str | float | int | None = None, capacity_mw: float | None = None) -> dict:
    """Physics parameter dict for a unit's class (see module docstring for fields)."""
    return dict(_CLASSES[class_name(palier, capacity_mw)])


def all_classes() -> dict[str, dict]:
    """The full class→physics table (for docs / validation)."""
    return {k: dict(v) for k, v in _CLASSES.items()}
