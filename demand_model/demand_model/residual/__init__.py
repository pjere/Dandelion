"""DM Phase 4 — stochastic residual layer (heteroscedastic seasonal-hourly AR)."""
from .model import ResidualModel, fit_residual_model

__all__ = ["ResidualModel", "fit_residual_model"]
