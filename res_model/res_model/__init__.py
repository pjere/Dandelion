"""res_model (step iv) — calibrated weather-to-power conversion for FR renewables.

Transforms the synthetic weather draws from step (ii) into hourly potential production for solar PV,
onshore wind, offshore wind and run-of-river hydro, under exogenous capacity scenarios. Consumes the
SAME weather draws as the demand model (step iii) so demand↔RES correlations propagate untouched.
"""
__version__ = "0.1.0"
