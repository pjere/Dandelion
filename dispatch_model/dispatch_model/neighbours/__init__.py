"""Neighbour-zone modules (§5): net load, RES, and aggregated dispatchable stacks per foreign zone.

Backtest mode uses ENTSO-E actuals (load, RES generation, per-tech generation → capacity proxy).
Projection mode (later) models demand/RES from the shared weather draws and takes capacities from
workbook TYNDP/ERAA trajectories. Same commodity SRMC as the FR stack, so a gas/CO2 shock moves all
zones coherently.
"""
