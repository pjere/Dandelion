"""Canonical marginal-tranche taxonomy + the single SRMC entry point (exogenous commodity/ETS prices).

The surrogate predicts **which tranche is marginal**, then reads the price off analytically. That split is
what lets a model trained on 2019-24 price a 2046 hour: the *classifier* only has to recognise a structural
state it has seen in ratio-space, while the *price level* comes from `tranche_srmc(tranche, prices)`
evaluated at that year's projected fuel/ETS prices.

**Commodity and ETS prices are a per-case exogenous input vector, never baked in.** Every fuel-dependent
number flows through `tranche_srmc`, so a finer acquisition of prompt fuel/EUA prices (or a better
efficiency source) is a change of *inputs and tranche definitions* here — not of the model architecture,
the features, or the trained weights. The same function also derives the training **label**, so sharper
prices sharpen the target as well as the features.

Tranche ids are **stable across 2019→2046**: they name a technology + efficiency band + support scheme,
never a unit. Fleet turnover changes which tranches *exist* and their capacities, not the label space.

Known crudeness inherited from `stacks/costs.py` (documented, and the reason this is injectable):
monthly-average commodity prices; per-unit efficiency drawn at random within a band (FR) or from
commissioning vintage (DE); lignite priced off the coal index; fixed oil MWh/bbl and USD/EUR; flat nuclear
fuel cost; no start-up/no-load/part-load terms (absorbed by the step-vii markup).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..stacks.costs import CO2_INTENSITY_TH, EFF_RANGE, FUEL_COMMODITY, VOM, fuel_eur_mwh_th, nuclear_srmc

#: exogenous price-vector keys every caller must supply (per zone, per hour or per period)
PRICE_KEYS = ("gas", "coal", "oil", "co2")

#: efficiency bands sampled per thermal tech — the label space. Finer heat-rate data later means more
#: bands (or per-unit tranches); the taxonomy is intentionally free to grow without touching the model.
_BANDS = ("lo", "mid", "hi")


@dataclass(frozen=True)
class Tranche:
    """One canonical, price-setting step of the merit order.

    `fuel`/`co2_intensity_th`/`efficiency` make the SRMC a pure function of the exogenous price vector;
    `fixed_price` short-circuits that for tranches whose price is a constant by construction (DSR, VoLL,
    scheme floors). `kind` drives the deferral checks and the per-regime evaluation split.
    """

    id: str
    kind: str                       # thermal | nuclear | hydro | res | dsr | scarcity | flex | import
    tech: str | None = None
    efficiency: float | None = None
    vom: float = 0.0
    fuel: str | None = None         # which PRICE_KEYS entry drives the fuel cost
    co2_intensity_th: float = 0.0
    fixed_price: float | None = None


def _band_efficiency(tech: str, band: str) -> float:
    lo, hi = EFF_RANGE.get(tech, (0.35, 0.45))
    return {"lo": lo, "mid": 0.5 * (lo + hi), "hi": hi}[band]


def thermal_tranches(techs=("gas", "coal", "lignite", "oil", "biomass")) -> list[Tranche]:
    """Fuel-priced tranches: one per (tech, efficiency band)."""
    out = []
    for tech in techs:
        for band in _BANDS:
            out.append(Tranche(id=f"{tech}.{band}", kind="thermal", tech=tech,
                               efficiency=_band_efficiency(tech, band), vom=VOM.get(tech, 2.5),
                               fuel=FUEL_COMMODITY.get(tech), co2_intensity_th=CO2_INTENSITY_TH.get(tech, 0.0)))
    return out


def canonical_tranches(res_floors: dict[str, float] | None = None,
                       dsr_prices=(300.0, 1000.0, 4000.0), voll: float = 15000.0,
                       flex_price: float = 180.0) -> list[Tranche]:
    """The full stable label space. `res_floors` = {scheme: bid_floor} from the workbook (negative-price
    tranches); DSR/VoLL/flex mirror the LP's scarcity ladder so both engines price scarcity identically."""
    out = list(thermal_tranches())
    out.append(Tranche(id="nuclear", kind="nuclear", tech="nuclear", vom=VOM.get("nuclear", 9.0)))
    out.append(Tranche(id="hydro_reservoir", kind="hydro", tech="hydro_reservoir",
                       vom=VOM.get("hydro_reservoir", 1.0)))
    for scheme, floor in (res_floors or {}).items():
        out.append(Tranche(id=f"res.{scheme}", kind="res", fixed_price=float(floor)))
    for i, pr in enumerate(dsr_prices):
        out.append(Tranche(id=f"dsr{i}", kind="dsr", fixed_price=float(pr)))
    out.append(Tranche(id="flex", kind="flex", tech="flex", fixed_price=float(flex_price)))
    out.append(Tranche(id="voll", kind="scarcity", fixed_price=float(voll)))
    # the price is set outside this zone (market coupling) — resolved to the setting zone's tranche
    out.append(Tranche(id="import", kind="import"))
    return out


def tranche_srmc(tr: Tranche, prices: dict[str, float], water_value: float | None = None) -> float:
    """**The single SRMC entry point.** €/MWh_el for `tr` under the exogenous price vector `prices`
    (keys: `PRICE_KEYS`, already zone-adjusted for gas basis by `commodities.model.zone_prices`).

    Refining commodity/ETS acquisition, FX, calorific values or efficiencies changes *this* call's inputs
    (or the tranche definitions) and nothing else in the surrogate. `water_value` supplies the
    hydro-reservoir opportunity cost, which is endogenous (the LP's budget dual) rather than a fuel cost.
    """
    if tr.fixed_price is not None:
        return float(tr.fixed_price)
    if tr.kind == "nuclear":
        return float(nuclear_srmc(prices))
    if tr.kind == "hydro":
        return float(water_value if water_value is not None else tr.vom)
    if tr.kind == "import":
        raise ValueError("'import' tranche has no own SRMC — resolve it to the price-setting zone first")
    if tr.fuel is None:                                   # biomass and other non-fuel thermal: VOM only
        return float(tr.vom)
    eff = float(tr.efficiency or 0.4)
    fuel = fuel_eur_mwh_th(tr.fuel, prices) / eff
    co2 = tr.co2_intensity_th / eff * float(prices["co2"])
    return float(fuel + co2 + tr.vom)


def srmc_vector(tranches: list[Tranche], prices: dict[str, float],
                water_value: float | None = None) -> dict[str, float]:
    """{tranche_id: SRMC} for one exogenous price vector — the merit order for a single hour/zone."""
    out = {}
    for tr in tranches:
        if tr.kind == "import":
            continue
        out[tr.id] = tranche_srmc(tr, prices, water_value=water_value)
    return out


def fuel_spreads(prices: dict[str, float], eff_gas: float = 0.50, eff_coal: float = 0.40) -> dict[str, float]:
    """Relative fuel economics — the features that actually determine merit *order*, and which generalise
    across price regimes far better than levels (critical for 2046).

    `clean_spark`/`clean_dark` are the CO2-inclusive generation costs of a reference CCGT/coal unit;
    `gas_coal_switch` is their difference (>0 ⇒ coal is in the money ahead of gas, i.e. the classic
    switching direction), and `switch_co2` the EUA price that would equalise them.
    """
    g = fuel_eur_mwh_th("gas", prices) / eff_gas + CO2_INTENSITY_TH["gas"] / eff_gas * prices["co2"]
    c = fuel_eur_mwh_th("coal", prices) / eff_coal + CO2_INTENSITY_TH["coal"] / eff_coal * prices["co2"]
    d_int = CO2_INTENSITY_TH["coal"] / eff_coal - CO2_INTENSITY_TH["gas"] / eff_gas
    fuel_only = fuel_eur_mwh_th("gas", prices) / eff_gas - fuel_eur_mwh_th("coal", prices) / eff_coal
    return {"clean_spark": g, "clean_dark": c, "gas_coal_switch": g - c,
            "switch_co2": (fuel_only / d_int) if abs(d_int) > 1e-9 else float("nan")}
