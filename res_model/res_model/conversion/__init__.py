"""res_model.conversion — physical conversion chains (recalibrated in Phase 4).

pv · wind_onshore · wind_offshore · hydro_ror. Each maps weather → a per-unit capacity-factor shape;
parameters are cohort/vintage-resolved so the national fleet CF evolves with the technology mix.
"""
from .hydro_ror import ror_cf
from .pv import pv_cf
from .wind_offshore import offshore_farm_cf
from .wind_onshore import aggregate_power_curve, onshore_cf

__all__ = ["pv_cf", "onshore_cf", "aggregate_power_curve", "offshore_farm_cf", "ror_cf"]
