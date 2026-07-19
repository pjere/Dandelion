"""Short-run marginal cost (SRMC) per technology from commodity prices — shared by FR + neighbour stacks.

    srmc_el = fuel_eur_per_mwh_th / efficiency_el
            + co2_t_per_mwh_th / efficiency_el * eua_eur_per_t
            + vom_eur_per_mwh_el

Efficiency dispersion across thermal units is what gives the mid-merit curve its slope, so callers pass
per-unit/per-block efficiencies rather than a single class value. CO2 intensities are per MWh_thermal.
"""
from __future__ import annotations

# tCO2 per MWh_thermal of fuel burnt
CO2_INTENSITY_TH = {"gas": 0.202, "coal": 0.340, "lignite": 0.364, "oil": 0.267, "biomass": 0.0}
# which commodity price drives each tech's fuel cost
FUEL_COMMODITY = {"gas": "gas", "coal": "coal", "lignite": "coal", "oil": "oil", "biomass": None}
# default electrical-efficiency ranges (min, max) for per-unit dispersion; class mid used if single-valued
EFF_RANGE = {
    "ccgt": (0.46, 0.60), "gas": (0.40, 0.58), "ocgt": (0.34, 0.42),
    "coal": (0.36, 0.46), "lignite": (0.35, 0.43), "oil": (0.30, 0.40), "biomass": (0.28, 0.38),
}
# default VOM (€/MWh_el)
VOM = {"nuclear": 9.0, "gas": 2.5, "ccgt": 2.5, "ocgt": 3.5, "coal": 3.5, "lignite": 3.5,
       "oil": 4.0, "biomass": 4.0, "hydro_reservoir": 1.0, "hydro_ror": 0.5, "hydro_psp": 1.0,
       "waste": 2.0, "solar": 0.0, "wind_onshore": 0.0, "wind_offshore": 0.0,
       # 2040 flexibility (battery + demand-response + H2-peaker) priced at its marginal cost — the peaking
       # backstop that caps scarcity at ~€180 instead of VoLL as firm thermal retires (#83).
       "flex": 180.0}
NUCLEAR_FUEL_EUR_MWH = 7.0            # fuel + variable O&M proxy for nuclear (workbook-overridable)
_OIL_MWHTH_PER_BBL = 1.7             # ~1.7 MWh_th per barrel
_USD_PER_EUR = 1.08


def fuel_eur_mwh_th(commodity: str, prices: dict[str, float]) -> float:
    """Commodity price in €/MWh_th. Oil (Brent $/bbl) is converted; gas/coal already €/MWh_th."""
    if commodity == "oil":
        return prices["oil"] / _OIL_MWHTH_PER_BBL / _USD_PER_EUR
    return prices[commodity]


def thermal_srmc(tech: str, efficiency: float, prices: dict[str, float], vom: float | None = None) -> float:
    """SRMC (€/MWh_el) for a thermal unit given its electrical efficiency and the month's commodity prices."""
    fuel_c = FUEL_COMMODITY.get(tech)
    if fuel_c is None:                                   # biomass / non-fuel: cost ≈ VOM only
        return float(vom if vom is not None else VOM.get(tech, 3.0))
    fuel = fuel_eur_mwh_th(fuel_c, prices) / efficiency
    co2 = CO2_INTENSITY_TH.get(tech, 0.0) / efficiency * prices["co2"]
    return float(fuel + co2 + (vom if vom is not None else VOM.get(tech, 2.5)))


def nuclear_srmc(prices: dict[str, float] | None = None, fuel: float = NUCLEAR_FUEL_EUR_MWH) -> float:
    return float(fuel)
