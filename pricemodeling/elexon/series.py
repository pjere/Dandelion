"""Elexon BMRS readers for GB, writing the ENTSO-E long schema so downstream code is unchanged.

See the package docstring for why GB needs its own upstream. Every function here reuses the ENTSO-E
ingest plumbing (`_long`, `_do`, `upsert_df`, the `ingest_log` idempotency) so a GB backfill behaves
exactly like an ENTSO-E one: resumable, per-chunk failures logged and skipped, re-runs retry.
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import pandas as pd
import requests

from ..db import ensure_rte_table
from ..entsoe.series import T_GEN, T_LOAD, T_PRICE, _do, _long

BASE = "https://data.elexon.co.uk/bmrs/api/v1"

#: BMRS fuel type → ENTSO-E PSR name. Written as the PSR name so `io.entsoe_hist.PSR2TECH` maps GB with
#: the same table as every other zone. CCGT and OCGT both fold into "Fossil Gas": the dispatch stack
#: separates them by efficiency, not by feed. INT* codes are interconnector flows, NOT generation, and
#: are deliberately excluded here — they belong in the flows table, and double-counting them as GB plant
#: would inflate the GB stack by ~10 GW.
FUEL2PSR = {
    "CCGT": "Fossil Gas", "OCGT": "Fossil Gas", "COAL": "Fossil Hard coal",
    "NUCLEAR": "Nuclear", "BIOMASS": "Biomass", "OIL": "Fossil Oil",
    "NPSHYD": "Hydro Run-of-river and poundage",     # non-pumped-storage hydro
    "PS": "Hydro Pumped Storage",
    "WIND": "Wind Onshore",                          # FUELINST does not split on/offshore; AGWS does
    "OTHER": "Other",
}
#: AGWS reports wind and solar by true PSR type, so it supersedes FUELINST's undifferentiated WIND and
#: supplies solar, which FUELINST omits entirely (GB solar is largely embedded/behind-the-meter).
AGWS_PSR = {"Solar": "Solar", "Wind Onshore": "Wind Onshore", "Wind Offshore": "Wind Offshore"}


def _get(path: str, params: dict, attempts: int = 5):
    """GET with backoff on transient upstream errors, mirroring `entsoe.series._fetch_retry`."""
    for i in range(attempts):
        try:
            r = requests.get(f"{BASE}{path}", params={**params, "format": "json"}, timeout=120)
            if r.status_code in (429, 500, 502, 503, 504) and i < attempts - 1:
                time.sleep(2 ** (i + 1)); continue
            r.raise_for_status()
            js = r.json()
            return js.get("data", js) if isinstance(js, dict) else js
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if i == attempts - 1:
                raise
            time.sleep(2 ** (i + 1))
    return []


def _day_chunks(start: date, end: date, days: int):
    cur = start
    while cur <= end:
        nxt = min(cur + timedelta(days=days - 1), end)
        yield cur, nxt
        cur = nxt + timedelta(days=1)


def _hourly(df: pd.DataFrame, tcol: str, vcol: str, keycol: str | None) -> pd.DataFrame:
    """BMRS publishes at 5-minute (FUELINST) or half-hourly (everything else) granularity; the model is
    hourly. Mean over the hour, which is the right statistic for a power series."""
    d = df.copy()
    d[tcol] = pd.to_datetime(d[tcol], utc=True, errors="coerce")
    d = d.dropna(subset=[tcol])
    d[vcol] = pd.to_numeric(d[vcol], errors="coerce")
    grp = [pd.Grouper(key=tcol, freq="h")] + ([keycol] if keycol else [])
    return d.groupby(grp, dropna=True)[vcol].mean().reset_index()


def ingest_generation(engine, start: date, end: date, force: bool = False) -> int:
    """FUELINST (conventional, by fuel) + AGWS (wind/solar by PSR) → `entsoe_generation`, series_key GB."""
    ensure_rte_table(engine, T_GEN)
    total = 0
    for c0, c1 in _day_chunks(start, end, 7):
        def build(raw, c0=c0):
            fuel, agws = raw
            frames = []
            if fuel:
                d = pd.DataFrame(fuel)
                d = d[d["fuelType"].isin(FUEL2PSR)]
                if not d.empty:
                    d["psr"] = d["fuelType"].map(FUEL2PSR)
                    h = _hourly(d, "startTime", "generation", "psr")
                    # CCGT+OCGT both map to Fossil Gas — sum after mapping, not before
                    h = h.groupby(["startTime", "psr"], as_index=False)["generation"].sum()
                    for psr, g in h.groupby("psr"):
                        frames.append(_long(g["startTime"], "GB", psr, psr, g["generation"]))
            if agws:
                d = pd.DataFrame(agws)
                col = "psrType" if "psrType" in d.columns else "businessType"
                d = d[d[col].isin(AGWS_PSR)]
                if not d.empty:
                    h = _hourly(d, "startTime", "quantity", col)
                    for psr, g in h.groupby(col):
                        frames.append(_long(g["startTime"], "GB", psr, psr, g["quantity"]))
            return frames

        total += _do(engine, lambda c0=c0, c1=c1: (
            _get("/datasets/FUELINST", {"publishDateTimeFrom": f"{c0}T00:00Z",
                                        "publishDateTimeTo": f"{c1 + timedelta(days=1)}T00:00Z"}),
            _get("/datasets/AGWS", {"publishDateTimeFrom": f"{c0}T00:00Z",
                                    "publishDateTimeTo": f"{c1 + timedelta(days=1)}T00:00Z"}),
        ), T_GEN, "elexon_gen", f"GB:{c0}", force, build)
    return total


def ingest_load(engine, start: date, end: date, force: bool = False) -> int:
    """Demand outturn → `entsoe_load`, series_key GB.

    ITSDO (transmission-system demand) is preferred over INDO: it includes station transformer load and
    pumping, which is the boundary the other zones' ENTSO-E load series uses. INDO is the fallback.
    """
    ensure_rte_table(engine, T_LOAD)
    total = 0
    for c0, c1 in _day_chunks(start, end, 14):
        def build(raw, c0=c0):
            if not raw:
                return []
            d = pd.DataFrame(raw)
            col = ("initialTransmissionSystemDemandOutturn"
                   if "initialTransmissionSystemDemandOutturn" in d.columns else "initialDemandOutturn")
            h = _hourly(d, "startTime", col, None)
            return [_long(h["startTime"], "GB", "load", "Actual Load", h[col])]

        total += _do(engine, lambda c0=c0, c1=c1: _get(
            "/demand/outturn", {"settlementDateFrom": str(c0), "settlementDateTo": str(c1)}),
            T_LOAD, "elexon_load", f"GB:{c0}", force, build)
    return total


def ingest_prices(engine, start: date, end: date, gbp_per_eur=None, force: bool = False) -> int:
    """MID (market index, APX) → `entsoe_day_ahead_prices`, series_key GB.

    BMRS quotes GBP/MWh while every other zone in the lake is EUR/MWh. `gbp_per_eur` must therefore be a
    callable date -> rate; WITHOUT it this function refuses to write rather than silently mixing
    currencies, which would corrupt every cross-zone comparison and the markup fit.

    N2EXMIDP rows are dropped: the feed carries both APX and N2EX providers and N2EX is frequently
    published as 0.00 with zero volume, which would halve the mean if averaged in blindly.
    """
    if gbp_per_eur is None:
        raise ValueError("ingest_prices needs `gbp_per_eur` (date -> rate): BMRS is GBP/MWh and the "
                         "lake is EUR/MWh. Refusing to write mixed currencies.")
    ensure_rte_table(engine, T_PRICE)
    total = 0
    for c0, c1 in _day_chunks(start, end, 14):
        def build(raw, c0=c0):
            if not raw:
                return []
            d = pd.DataFrame(raw)
            if "dataProvider" in d.columns:
                d = d[d["dataProvider"].astype(str).str.startswith("APX")]
            d = d[pd.to_numeric(d.get("volume", 1), errors="coerce").fillna(1) > 0]
            if d.empty:
                return []
            h = _hourly(d, "startTime", "price", None)
            rate = pd.Series([gbp_per_eur(t.date()) for t in h["startTime"]], index=h.index)
            return [_long(h["startTime"], "GB", "price", "Day-ahead Price", h["price"] / rate)]

        total += _do(engine, lambda c0=c0, c1=c1: _get(
            "/balancing/pricing/market-index", {"from": f"{c0}T00:00Z",
                                                "to": f"{c1 + timedelta(days=1)}T00:00Z"}),
            T_PRICE, "elexon_price", f"GB:{c0}", force, build)
    return total
