"""Short-run marginal cost (SRMC) per technology from commodity prices — shared by FR + neighbour stacks.

    srmc_el = fuel_eur_per_mwh_th / efficiency_el
            + co2_t_per_mwh_th / efficiency_el * eua_eur_per_t
            + vom_eur_per_mwh_el

Efficiency dispersion across thermal units is what gives the mid-merit curve its slope, so callers pass
per-unit/per-block efficiencies rather than a single class value. CO2 intensities are per MWh_thermal.
"""
from __future__ import annotations

import os

# tCO2 per MWh_thermal of fuel burnt
CO2_INTENSITY_TH = {"gas": 0.202, "coal": 0.340, "lignite": 0.364, "oil": 0.267, "biomass": 0.0}
#: German/Polish/Czech lignite is MINE-MOUTH: dug in an opencast pit beside the plant, moved by conveyor,
#: never traded and never shipped. Its cost is a production cost of roughly 1.2-1.8 €/GJ, i.e. ~4.3-6.5
#: €/MWh_th, and it barely moves from year to year — it has no exposure to a seaborne market.
#:
#: IT WAS PRICED OFF THE SEABORNE COAL INDEX, which in a fuel crisis is not a small error. In July 2022
#: the model charged lignite 37.9 €/MWh_th — about 264 €/t of hard coal — for fuel that costs ~5. The
#: symptom is visible in this repo's own Phase-3 note (`DECISIONS.md`), recorded at the time without
#: being recognised: "2022 gas €340 » coal €147 / lignite €159". **Lignite above hard coal is an
#: inversion**: lignite has the worse efficiency and the higher CO2 intensity but by far the cheaper fuel,
#: so it belongs below coal in every European merit order, which is why German lignite runs baseload.
#:
#: 5.0 is the mid of the production-cost range. A per-zone/per-year refinement would need mine accounts;
#: the constant is right to within a euro or two and, crucially, is not a market price.
LIGNITE_FUEL_EUR_MWH_TH = 5.0

# which commodity price drives each tech's fuel cost
FUEL_COMMODITY = {"gas": "gas", "coal": "coal", "lignite": "lignite", "oil": "oil", "biomass": None}
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
       # backstop that caps scarcity instead of VoLL as firm thermal retires (#83). Raised 180 -> 300 by
       # the workbook owner. Still a DEFAULT, not a revealed-behaviour measurement, and it is now
       # load-bearing: until the NaN in `_append_flex` was fixed (commit 236aa7f) this block never
       # dispatched at all, so this number sets the scarcity ceiling in every tight zone of every
       # projected year for the first time. Being battery + DR + H2-peaker, it should eventually come
       # through the same revealed route as every other bid in this model.
       "flex": 300.0}
NUCLEAR_FUEL_EUR_MWH = 7.0            # fuel + variable O&M proxy for nuclear (workbook-overridable)
_OIL_MWHTH_PER_BBL = 1.7             # ~1.7 MWh_th per barrel
_USD_PER_EUR = 1.08


def fuel_eur_mwh_th(commodity: str, prices: dict[str, float]) -> float:
    """Commodity price in €/MWh_th. Oil (Brent $/bbl) is converted; gas/coal already €/MWh_th.

    `lignite` is not a traded commodity and the resolver does not carry one, so it falls back to the
    mine-mouth production cost (see `LIGNITE_FUEL_EUR_MWH_TH`). A caller may still inject a `lignite`
    key — the workbook route stays open — but absent one this must NOT reach for the coal price.
    Set `DISPATCH_LIGNITE_FUEL=0` to restore the old seaborne-coal behaviour for A/B.
    """
    if commodity == "oil":
        return prices["oil"] / _OIL_MWHTH_PER_BBL / _USD_PER_EUR
    if commodity == "lignite":
        if os.environ.get("DISPATCH_LIGNITE_FUEL", "1") in ("0", "false", "False"):
            return prices["coal"]
        return float(prices.get("lignite", LIGNITE_FUEL_EUR_MWH_TH))
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
