"""Price resolution — which fuel/ETS price a given hour actually sees.

Precedence, per commodity independently: **daily observed → monthly observed → scenario trajectory**.
Historical hours get the real dated price at the finest granularity ingested; projection years, where no
observation can exist, fall back to `CommodityModel`'s annual-level × seasonal-shape trajectory. Mixed
states are normal and supported — e.g. daily EUA + monthly coal + scenario gas beyond 2025 — and
`explain()` reports exactly which source each commodity resolved to, so a backtest never silently prices
against synthetic fuel.

This is the single read path for the exogenous price vector: `prices_at(ts)` replaces the old
`rolling.assemble._month_prices`, and everything downstream (`stacks.fr_stack.srmc`,
`surrogate.tranches.tranche_srmc`, the labels) consumes its output unchanged.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .model import COMMODITIES, CommodityModel
from .observed import read_observed


class PriceResolver:
    """Resolves the exogenous price vector for any timestamp, preferring observed data.

    Build once per run (it caches the observed series and the scenario trajectory) and call `prices_at`
    or the vectorised `frame_for` per window.
    """

    def __init__(self, model: CommodityModel, observed: pd.DataFrame | None = None,
                 prefer: tuple[str, ...] = ("daily", "monthly")):
        self.model = model
        self.prefer = prefer
        obs = read_observed() if observed is None else observed
        self._series: dict[tuple[str, str], pd.Series] = {}
        if not obs.empty:
            o = obs.copy()
            o["date"] = pd.to_datetime(o["date"])
            for (com, gran), g in o.groupby(["commodity", "granularity"]):
                # one source per (commodity, granularity): the most recently-ending series wins
                best = g.groupby("source")["date"].max().idxmax()
                s = (g[g["source"] == best].drop_duplicates("date")
                     .set_index("date")["price"].astype(float).sort_index())
                self._series[(str(com), str(gran))] = s
        self._scen_cache: dict[int, pd.DataFrame] = {}

    # ---- scenario fallback ---------------------------------------------------
    def _scenario_month(self, year: int) -> pd.DataFrame:
        if year not in self._scen_cache:
            m = self.model.monthly_prices(year, year)
            m["month"] = pd.to_datetime(m["date"]).dt.month
            self._scen_cache[year] = m
        return self._scen_cache[year]

    def _scenario_value(self, com: str, ts: pd.Timestamp) -> float:
        m = self._scenario_month(int(ts.year))
        row = m[(m["commodity"] == com) & (m["month"] == int(ts.month))]
        return float(row["price"].iloc[0]) if len(row) else float("nan")

    # ---- resolution ----------------------------------------------------------
    def _observed_value(self, com: str, ts: pd.Timestamp) -> tuple[float, str] | None:
        """As-of lookup at the finest preferred granularity (no forward-filling across a gap > 31 d)."""
        day = pd.Timestamp(ts).tz_localize(None).normalize()
        for gran in self.prefer:
            s = self._series.get((com, gran))
            if s is None or s.empty:
                continue
            i = s.index.searchsorted(day, side="right") - 1     # last observation at or before `day`
            if i < 0:
                continue
            stamp = s.index[i]
            if (day - stamp).days > 31:                          # stale: fall through to a coarser source
                continue
            return float(s.iloc[i]), gran
        return None

    def prices_at(self, ts) -> dict[str, float]:
        """The exogenous price vector for `ts` — keys `gas, coal, oil, co2` in canonical units."""
        ts = pd.Timestamp(ts)
        out = {}
        for com in COMMODITIES:
            hit = self._observed_value(com, ts)
            out[com] = hit[0] if hit else self._scenario_value(com, ts)
        return out

    def explain(self, ts) -> dict[str, str]:
        """{commodity: 'daily:<source>' | 'monthly:<source>' | 'scenario'} — provenance for `ts`."""
        ts = pd.Timestamp(ts)
        out = {}
        for com in COMMODITIES:
            hit = self._observed_value(com, ts)
            out[com] = f"{hit[1]}:observed" if hit else "scenario"
        return out

    def frame_for(self, index) -> pd.DataFrame:
        """Vectorised price vector over an hourly index → DataFrame[gas, coal, oil, co2].

        Resolved once per distinct **day** (fuel is not hourly) and broadcast back to the hours, so a
        168-hour window costs 7 lookups per commodity rather than 168.
        """
        idx = pd.DatetimeIndex(index)
        days = pd.DatetimeIndex(pd.Series(idx).dt.tz_localize(None).dt.normalize().unique()).sort_values()
        per_day = pd.DataFrame([self.prices_at(d) for d in days], index=days, columns=list(COMMODITIES))
        key = pd.Series(idx).dt.tz_localize(None).dt.normalize().to_numpy()
        out = per_day.reindex(key)
        out.index = idx
        return out

    def coverage_report(self, start, end) -> pd.DataFrame:
        """Share of days in [start, end] resolved from observed data vs the scenario, per commodity —
        run this before trusting a historical backtest's fuel costs."""
        days = pd.date_range(pd.Timestamp(start).tz_localize(None).normalize(),
                             pd.Timestamp(end).tz_localize(None).normalize(), freq="D")
        rows = []
        for com in COMMODITIES:
            hits = [self._observed_value(com, d) for d in days]
            gran = [h[1] if h else "scenario" for h in hits]
            rows.append({"commodity": com, "n_days": len(days),
                         "pct_daily": 100 * np.mean([g == "daily" for g in gran]),
                         "pct_monthly": 100 * np.mean([g == "monthly" for g in gran]),
                         "pct_scenario": 100 * np.mean([g == "scenario" for g in gran])})
        return pd.DataFrame(rows)


def build_resolver(workbook, observed: pd.DataFrame | None = None) -> PriceResolver:
    """Convenience: scenario model from the workbook + whatever observed series have been ingested."""
    return PriceResolver(CommodityModel.from_workbook(workbook), observed=observed)
