"""DM Phase 5 — turn the assumptions workbook into per-year component factors + raw driver series.

Component factors are **multiplicative, ==1 at the anchor year**, so they rescale the calibrated
statistical components without changing their weather/calendar shape:

* ``base``  = structural index (population + tertiary/GDP + industry) × autonomous-efficiency erosion
* ``heat``  = electric-heating index  S = (resistance_stock + hp_stock/COP) × renovation_index
              → captures resistance→HP substitution (÷COP), new HP electrification, and renovation
* ``cool``  = AC penetration ratio
* ``light`` = population ratio (activity proxy)

Raw driver series (EV fleet, electrolysis, PV, …) are exposed for the bottom-up modules.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Drivers:
    sheets: dict[str, pd.DataFrame]
    scenario: str
    anchor_year: int
    base_shares: dict[str, float]
    years: np.ndarray               # projection horizon years (inclusive)
    cop_cold_derate: float | None = None   # override: force a constant cold-COP derate; None → COP(T) curve

    # ------------------------------------------------------------------ access
    def series(self, sheet: str, variable: str) -> pd.Series:
        """Yearly series for one variable, reindexed onto {anchor} ∪ horizon and interpolated.

        Falls back to the sole scenario present if the requested one is absent on that sheet."""
        df = self.sheets[sheet]
        sub = df[df["variable"] == variable]
        if sub.empty:
            raise KeyError(f"driver '{variable}' not found on sheet '{sheet}'")
        if "scenario" in sub.columns and self.scenario in set(sub["scenario"]):
            sub = sub[sub["scenario"] == self.scenario]
        s = sub.set_index("year")["value"].sort_index()
        s = s[~s.index.duplicated(keep="last")]
        full = np.union1d(self.years, [self.anchor_year])
        return s.reindex(full).interpolate("index").ffill().bfill()

    def at(self, sheet: str, variable: str) -> pd.Series:
        return self.series(sheet, variable).reindex(self.years)

    def _ratio(self, sheet: str, variable: str) -> pd.Series:
        s = self.series(sheet, variable)
        return (s / s.loc[self.anchor_year]).reindex(self.years)

    # ------------------------------------------------------------------ component factors
    def factor_base(self) -> pd.Series:
        pop = self._ratio("demography", "population")
        floor = self._ratio("residential_tertiary", "floor_area_index")
        gdp = self._ratio("macro", "gdp_index")
        ind = self._industry_ratio()
        w = self.base_shares
        struct = w["residential"] * (0.5 * pop + 0.5 * floor) + w["tertiary"] * gdp + w["industry"] * ind
        return (struct * self._efficiency_factor()).rename("base")

    def factor_heat(self) -> pd.Series:
        res = self.series("residential_tertiary", "resistance_heating_stock")
        hp = self.series("residential_tertiary", "heat_pump_stock")
        cop = self.series("residential_tertiary", "hp_cop_avg")
        renov = self.series("residential_tertiary", "renovation_specific_demand_index")
        # HP electricity for the heating GRADIENT is drawn in cold weather, where the COP collapses — so the
        # heating sensitivity uses the cold-weather COP, not the annual SCOP. That derate comes from a
        # physically-grounded COP(T) curve (#40, heatpump.py) whose steepness depends on the fleet's SCOP, so
        # as the workbook's hp_cop_avg rises (fleet modernisation) the derate *improves* — instead of the old
        # static 0.62 scalar (kept as an optional override). Reproduces 0.62 at today's SCOP-2.8, so the
        # calibration is preserved; only the fleet-evolution of the derate is new.
        if self.cop_cold_derate is not None:
            derate = self.cop_cold_derate
        else:
            from .heatpump import cold_derate
            derate = pd.Series(cold_derate(cop.to_numpy()), index=cop.index)
        cop_heat = cop * derate
        s = (res + hp / cop_heat) * renov                 # electric-heating sensitivity index
        return (s / s.loc[self.anchor_year]).reindex(self.years).rename("heat")

    def factor_cool(self) -> pd.Series:
        return self._ratio("residential_tertiary", "ac_penetration").rename("cool")

    def factor_light(self) -> pd.Series:
        return self._ratio("demography", "population").rename("light")

    def component_factors(self) -> pd.DataFrame:
        f = pd.concat([self.factor_base(), self.factor_heat(), self.factor_cool(),
                       self.factor_light()], axis=1)
        f.index.name = "year"
        return f

    # ------------------------------------------------------------------ helpers
    def _industry_ratio(self) -> pd.Series:
        sectors = [v for v in self.sheets["macro"]["variable"].unique()
                   if v.endswith("_index") and v != "gdp_index"]
        if not sectors:
            return pd.Series(1.0, index=self.years)
        return pd.concat([self._ratio("macro", v) for v in sectors], axis=1).mean(axis=1)

    def _efficiency_factor(self) -> pd.Series:
        """Cumulative (1 - autonomous_efficiency_rate) from anchor+1 to each year."""
        rate = self.series("efficiency", "autonomous_efficiency_rate")
        out = {}
        for y in self.years:
            span = range(self.anchor_year + 1, int(y) + 1)
            out[y] = float(np.prod([1.0 - rate.loc[k] for k in span])) if y > self.anchor_year else 1.0
        return pd.Series(out).reindex(self.years)
