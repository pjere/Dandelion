"""DM Phase 5 — structural projection layer.

Evaluates the calibrated statistical core on synthetic weather (weathergen), rescales each
separable component by scenario drivers, adds bottom-up new loads (EV/H2/datacentre), nets
behind-the-meter PV, and overlays the stochastic residual layer.
"""
from .drivers import Drivers
from .engine import Projector, project_scenario, project_trajectory, run_projection

__all__ = ["Drivers", "Projector", "project_scenario", "project_trajectory", "run_projection"]
