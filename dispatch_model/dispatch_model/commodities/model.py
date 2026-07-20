"""Commodity-price generator (§6): annual trajectories → monthly prices, deterministic or stochastic.

One source of truth consumed by the French stack and every neighbour stack, so a gas/CO2 shock moves all
zones coherently. Deterministic path = annual level (linearly interpolated/extended) × per-commodity
monthly seasonal shape. Stochastic path adds correlated mean-reverting (Ornstein–Uhlenbeck, on log-price)
monthly deviations with a gas/coal/CO2 correlation matrix, seeded per draw (behind a config flag).

Units: gas & coal in €/MWh_th, CO2 (EUA) in €/t, oil (Brent) in $/bbl. The stack module (Phase 4) turns
these into €/MWh_el via unit efficiencies and CO2 intensities. Marginal-cost identity, e.g. gas plant:
    srmc = gas_eur_mwhth / eff + co2_t_per_mwhth_gas / eff * eua_eur_t + vom.

Backtest-year annual levels are seeded from public annual averages (TTF/EUA/API2/Brent). Monthly
historical commodity prices are a documented refinement (2022's within-year gas explosion is only
approximated by the seasonal shape) — sourcing them tightens the crisis-year backtest.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

COMMODITIES = ("gas", "co2", "coal", "oil")

# public annual averages (approx): TTF €/MWh_th, EUA €/t, API2 coal €/MWh_th, Brent $/bbl.
# Backtest years + a flat-ish forward reference scenario (user-overridable in the workbook).
_DEFAULT_ANNUAL = {
    "gas":  {2019: 13.5, 2020: 9.5, 2021: 46.0, 2022: 123.0, 2023: 41.0, 2024: 34.0, 2027: 28.0, 2046: 25.0},
    "co2":  {2019: 24.8, 2020: 24.5, 2021: 53.0, 2022: 81.0, 2023: 83.7, 2024: 65.0, 2027: 85.0, 2046: 140.0},
    "coal": {2019: 7.6, 2020: 6.4, 2021: 12.5, 2022: 30.0, 2023: 15.0, 2024: 12.0, 2027: 11.0, 2046: 10.0},
    "oil":  {2019: 64.0, 2020: 42.0, 2021: 71.0, 2022: 101.0, 2023: 82.0, 2024: 80.0, 2027: 78.0, 2046: 75.0},
}
# monthly seasonal shape (mean 1). Gas has a winter premium; CO2/coal/oil ~flat.
_DEFAULT_SHAPE = {
    "gas":  [1.18, 1.15, 1.08, 0.95, 0.88, 0.85, 0.86, 0.88, 0.94, 1.02, 1.10, 1.16],
    "co2":  [1.0] * 12, "coal": [1.02, 1.02, 1.0, 0.99, 0.98, 0.98, 0.99, 1.0, 1.0, 1.0, 1.01, 1.02],
    "oil":  [1.0] * 12,
}
# OU on log-price: theta (monthly mean-reversion), sigma (monthly vol). Correlation gas/co2/coal/oil.
_DEFAULT_OU = {
    "theta": {"gas": 0.25, "co2": 0.15, "coal": 0.25, "oil": 0.20},
    "sigma": {"gas": 0.18, "co2": 0.12, "coal": 0.14, "oil": 0.10},
    "corr": [[1.00, 0.55, 0.60, 0.45],       # gas
             [0.55, 1.00, 0.40, 0.30],       # co2
             [0.60, 0.40, 1.00, 0.50],       # coal
             [0.45, 0.30, 0.50, 1.00]],      # oil
}


def _interp_annual(levels: dict[int, float], years: np.ndarray) -> np.ndarray:
    ys = np.array(sorted(levels)); vs = np.array([levels[y] for y in ys], float)
    return np.interp(years, ys, vs)                                 # flat extrapolation outside range


# --- per-zone gas hub basis ------------------------------------------------ #
# Zones do not all burn TTF gas: IT-North prices off PSV and ES off MIBGAS, which ran €2-5/MWh_th above
# TTF. Feeding every zone a single TTF price systematically under-costs their gas plant (and so their
# SMC). This is a **missing input**, not a behavioural wedge — it belongs here, not in the step-(vii)
# markup, otherwise the markup would absorb it as a fake "Italian premium" and carry it into every
# future scenario.
def load_zone_basis(path) -> dict[str, float]:
    """{zone: gas basis €/MWh_th over TTF} from the `dispatch_zone_basis` tab; {} if absent."""
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return {}
    try:
        from powersim_core.scenario import load_sheet
        df = load_sheet(p, "dispatch", "zone_basis")
    except (ValueError, KeyError):
        return {}
    g = df[df["variable"] == "gas_basis_eur_mwhth"]
    return {str(z): float(v) for z, v in zip(g["zone"], g["value"], strict=False)}


def zone_prices(prices: dict, zone: str, basis: dict, ts=None, gas_rules=None) -> dict:
    """Commodity prices as seen by `zone` — gas at its own hub (TTF + basis); others are EU-wide.

    With `ts` and `gas_rules` (see `commodities.gas_rules`) the gas price additionally honours
    period-specific rules: a time-varying hub basis and any regulatory ceiling on gas-for-power (the
    Iberian exception capped ES gas from 15-Jun-2022, so Spanish CCGTs set the price off a capped fuel
    cost while TTF traded far higher). Without them the behaviour is the original flat basis.
    """
    b = float(basis.get(zone, 0.0))
    if gas_rules:
        from .gas_rules import adjust_gas
        gas = adjust_gas(prices["gas"], zone, ts, gas_rules, flat_basis=b)
        return {**prices, "gas": gas}
    return prices if b == 0.0 else {**prices, "gas": prices["gas"] + b}


@dataclass
class CommodityModel:
    annual: dict = field(default_factory=lambda: {k: dict(v) for k, v in _DEFAULT_ANNUAL.items()})
    shape: dict = field(default_factory=lambda: {k: list(v) for k, v in _DEFAULT_SHAPE.items()})
    ou: dict = field(default_factory=lambda: _DEFAULT_OU)

    def monthly_prices(self, start_year: int, end_year: int, draw: int = 0,
                       stochastic: bool = False, seed: int = 0) -> pd.DataFrame:
        """→ long DataFrame [date (month start, UTC), commodity, price]. Prices are strictly positive."""
        dates = pd.date_range(f"{start_year}-01-01", f"{end_year}-12-01", freq="MS", tz="UTC")
        yr = dates.year.to_numpy(); mo = dates.month.to_numpy()
        n = len(dates)
        # deterministic level × seasonal shape
        base = {}
        for c in COMMODITIES:
            lvl = _interp_annual(self.annual[c], yr)
            base[c] = lvl * np.array([self.shape[c][m - 1] for m in mo])
        dev = {c: np.zeros(n) for c in COMMODITIES}
        if stochastic:
            dev = self._ou_deviations(n, draw, seed)
        rows = []
        for c in COMMODITIES:
            price = base[c] * np.exp(dev[c])
            rows.append(pd.DataFrame({"date": dates, "commodity": c, "price": np.maximum(price, 1e-3)}))
        return pd.concat(rows, ignore_index=True)

    def _ou_deviations(self, n: int, draw: int, seed: int) -> dict:
        from powersim_core.rng import substream
        rng = substream(seed, draw, "commodity_ou")           # F4: single RNG authority (SeedSequence)
        L = np.linalg.cholesky(np.array(self.ou["corr"], float))
        theta = np.array([self.ou["theta"][c] for c in COMMODITIES])
        sigma = np.array([self.ou["sigma"][c] for c in COMMODITIES])
        x = np.zeros(len(COMMODITIES))
        out = np.zeros((n, len(COMMODITIES)))
        for t in range(n):
            z = L @ rng.standard_normal(len(COMMODITIES))            # correlated innovations
            x = x - theta * x + sigma * z                           # OU step (mean 0 in log-space)
            out[t] = x
        return {c: out[:, i] for i, c in enumerate(COMMODITIES)}

    # ---- workbook I/O -------------------------------------------------------
    @classmethod
    def from_workbook(cls, path) -> CommodityModel:
        """Read the `dispatch_commodities` tab (long: year, commodity, price) from the merged
        scenarios.xlsx if present; else fall back to the built-in defaults."""
        from pathlib import Path
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            from powersim_core.scenario import load_sheet
            df = load_sheet(p, "dispatch", "commodities")
        except (ValueError, KeyError):
            return cls()
        annual = {c: dict(zip(g["year"].astype(int), g["price"].astype(float)))
                  for c, g in df.groupby("commodity")}
        return cls(annual={**{k: dict(v) for k, v in _DEFAULT_ANNUAL.items()}, **annual})
