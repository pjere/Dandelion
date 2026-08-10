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
from ..entsoe.series import T_FLOW, T_GEN, T_LOAD, T_PRICE, _do, _long

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
    "OTHER": "Other",
    # WIND is deliberately ABSENT. FUELINST reports one undifferentiated wind figure, and AGWS reports
    # the same fleet split by PSR type — both would land on sub_key "Wind Onshore" and collide on the
    # upsert key (ts_utc, series_key, sub_key). It happened to resolve correctly, AGWS being appended
    # second and overwriting, but that is frame ordering rather than intent: reorder the build and the
    # split silently becomes an unsplit total. AGWS is the only wind source.
}
#: AGWS reports wind and solar by true PSR type, so it supersedes FUELINST's undifferentiated WIND and
#: supplies solar, which FUELINST omits entirely (GB solar is largely embedded/behind-the-meter).
AGWS_PSR = {"Solar": "Solar", "Wind Onshore": "Wind Onshore", "Wind Offshore": "Wind Offshore"}

#: FUELINST INT* code → the lake zone on the far side of that interconnector.
#:
#: Complete for reference; `ingest_flows` writes only `VIKING_ONLY` by default. Measured p99.5 of hourly
#: flow over 2024, import / export MW, against nameplate:
#:     INTFR   IFA       2008 / 1955   (2000)      INTNED  BritNed   1055 / 1047   (1000)
#:     INTIFA2 IFA2       992 /  886   (1000)      INTVKL  Viking    1424 / 1097   (1400)
#:     INTELEC ElecLink   999 /  705   (1000)      INTNSL  NSL       1399 / 1240   (1400)
#:     INTNEM  Nemo      1019 / 1021   (1000)      INTIRL/INTEW/INTGRNL → Ireland
INT_LINKS = {
    "INTFR": "FR", "INTIFA2": "FR", "INTELEC": "FR",   # IFA + IFA2 + ElecLink
    "INTNEM": "BE",                                     # Nemo Link
    "INTNED": "NL",                                     # BritNed
    "INTVKL": "DK_1",                                   # Viking Link (commissioned 2023-12-29)
    "INTNSL": "NO_2", "INTIRL": "IE", "INTEW": "IE", "INTGRNL": "IE",   # zones not in the model
}

#: The GB borders ENTSO-E does NOT publish, and therefore the only ones Elexon may write.
#:
#: `entsoe_flows` already carries FR>GB, BE>GB and (from 2024) NL>GB, published from the continental side.
#: Those share the upsert key (ts_utc, series_key, sub_key) with anything written here, so ingesting them
#: from Elexon would silently REPLACE the ENTSO-E measurement with a different one — metered interconnector
#: output rather than scheduled physical flow. Same border, different quantity, no warning. Viking Link is
#: absent from ENTSO-E in every year, so it is the one GB border with nothing to overwrite.
VIKING_ONLY = ("INTVKL",)


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


def ingest_flows(engine, start: date, end: date, codes=VIKING_ONLY, force: bool = False) -> int:
    """FUELINST INT* → `entsoe_flows`, as directed non-negative series (`DK_1>GB`, `GB>DK_1`).

    Why this exists: promoting GB to a modelled zone needs an NTC per border, and `flow_derived_ntc`
    derives those from realised flow, falling back to the static `NTC` default only where flow history is
    missing. Viking Link has NO flow history in the lake from any source, so without this its static
    default would bind in EVERY year — including 2019 and 2022, when the link did not physically exist.
    The static default is therefore 0 and this supplies the real capacity for the years Viking has run,
    which is the honest split: absent where absent, measured where measured.

    FUELINST signs flow positive-into-GB. ENTSO-E's convention is one non-negative series per direction,
    so the sign is split across two keys rather than stored as a signed quantity.

    `codes` defaults to `VIKING_ONLY` — see that constant for why the other GB interconnectors are barred.
    """
    ensure_rte_table(engine, T_FLOW)
    total = 0
    for c0, c1 in _day_chunks(start, end, 7):
        def build(raw, c0=c0):
            if not raw:
                return []
            d = pd.DataFrame(raw)
            d = d[d["fuelType"].isin(codes)]
            if d.empty:
                return []
            d["zone"] = d["fuelType"].map(INT_LINKS)
            h = _hourly(d, "startTime", "generation", "zone")
            # several codes can share a zone (IFA/IFA2/ElecLink → FR); sum them into one border
            h = h.groupby(["startTime", "zone"], as_index=False)["generation"].sum()
            frames = []
            for z, g in h.groupby("zone"):
                v = g["generation"]
                frames.append(_long(g["startTime"], f"{z}>GB", "", "flow_mw", v.clip(lower=0)))
                frames.append(_long(g["startTime"], f"GB>{z}", "", "flow_mw", (-v).clip(lower=0)))
            return frames

        total += _do(engine, lambda c0=c0, c1=c1: _get(
            "/datasets/FUELINST", {"publishDateTimeFrom": f"{c0}T00:00Z",
                                   "publishDateTimeTo": f"{c1 + timedelta(days=1)}T00:00Z"}),
            T_FLOW, "elexon_flow", f"GB:{c0}", force, build)
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
    # 7-day chunks, NOT the 14 used for generation and demand: this endpoint alone rejects a longer
    # window. Measured against the live service — 1/3/7 days return 200, 14 returns HTTP 400 — which is
    # why a first pass landed only 49 price hours for 2024 while gen and load came back complete.
    for c0, c1 in _day_chunks(start, end, 7):
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
