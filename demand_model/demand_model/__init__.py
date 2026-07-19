"""demand_model — hybrid statistical-structural long-term hourly power-demand model (France).

Statistical core (calendar + thermosensitive + lighting + residual) calibrated on history,
a structural projection layer that rescales/reshapes each component from scenario drivers and
adds bottom-up new-load modules (EV, heat pumps, electrolysis, data centres, BTM-PV), and a
stochastic residual layer. Consumes the weather generator's synthetic draws for projection.
"""

__version__ = "0.1.0"
