"""DM/RES Phase 5 — stochastic residual layer (heteroscedastic-by-CF, AR, cross-tech)."""
from .fit import fit_residual_model
from .model import ResidualModel

__all__ = ["fit_residual_model", "ResidualModel"]
