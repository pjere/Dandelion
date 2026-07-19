"""availability_model (step v) — unit-level stochastic availability of the FR dispatchable fleet.

Generates hourly available-capacity series per generating unit over a 10–20 year horizon and many
Monte-Carlo draws, combining planned (maintenance/refuelling) unavailability, forced outages with a
long-term trend, common-mode (generic-fault) events across nuclear paliers, weather-linked deratings
(river-cooled thermal), and stochastic hydro inflows — consuming the SAME weather draws as steps
(iii)/(iv). Output feeds the dispatch/price model of step (vi).
"""
__version__ = "0.1.0"
