"""weathergen — multi-site hourly stochastic weather generator.

Fit-once / simulate-many. Reproduces marginals, diurnal+seasonal cycles, temporal
autocorrelation and inter-station + cross-variable dependence, with EVT tails that
extrapolate and an externally-imposed climate trend.

The heavy statistical stack (statsmodels, pyextremes, xclim, scikit-learn) is imported
lazily inside the phase modules that use it, so the scaffold/smoke path stays light.
"""

__version__ = "0.1.0"
