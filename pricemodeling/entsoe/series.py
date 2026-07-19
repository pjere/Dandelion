"""ENTSO-E load / generation / cross-border flows / NTC -> long-schema DB tables (step vi inputs).

Complements `prices.py` (day-ahead prices). Uses `entsoe-py` (robust XML/EIC/PSR handling) but writes to
the project's standard long schema (ts_utc, ts_end_utc, series_key, sub_key, label, value) via the shared
db helpers, so these sit alongside the rte_* tables and feed the dispatch model's neighbour calibration
and historical backtests. Yearly chunks + ingest_log make re-runs idempotent.

Zones (7-zone dispatch model): FR, DE_LU, BE, GB, CH, IT_NORD, ES. Second-ring folded in later.
"""
from __future__ import annotations

import time
from datetime import date

import pandas as pd
import requests

from ..db import already_ingested, ensure_rte_table, log_ingest, upsert_df

# my zone code -> entsoe-py area code
ZONES = {"FR": "FR", "DE_LU": "DE_LU", "BE": "BE", "GB": "GB", "CH": "CH",
         "IT_NORTH": "IT_NORD", "ES": "ES"}

# --- DE_REST constituents (data-only; aggregated into ONE virtual dispatch zone) ---------------
# The 7-zone model gives DE-LU only ~8 GW of export headroom (FR 3 + BE 1 + CH 4), but the observed
# 210 negative hours in 2019 imply ~14.5 GW. The gap is DE's NL/AT/DK/PL/CZ borders, which simply do
# not exist in the zone set — so DE's surplus is trapped, it prices itself to the RES bid for 665 h,
# and the negative prices never propagate to FR/BE/CH (85 %/79 %/82 % of whose observed negatives are
# DE-coincident). These are ingested and aggregated into a single price-responsive `DE_REST` zone
# rather than modelled individually: one extra zone buys back the missing headroom and keeps the
# projection valid (an exogenous export schedule would not). Their own external borders (NO/SE/SK/HU)
# are out of scope — a documented v1 boundary simplification.
DE_REST_ZONES = {"NL": "NL", "AT": "AT", "DK_1": "DK_1", "DK_2": "DK_2", "PL": "PL", "CZ": "CZ"}
ALL_ZONES = {**ZONES, **DE_REST_ZONES}

# coupling graph among the 7 zones (undirected; both directions fetched)
BORDERS = [("FR", "DE_LU"), ("FR", "BE"), ("FR", "GB"), ("FR", "CH"), ("FR", "IT_NORTH"),
           ("FR", "ES"), ("DE_LU", "BE"), ("DE_LU", "CH"), ("CH", "IT_NORTH"), ("BE", "GB")]
# DE-LU ↔ DE_REST constituents: the missing headroom. (DE/AT were one bidding zone until Oct-2018,
# so the DE_LU-AT border only carries flow from then on.)
DE_REST_BORDERS = [("DE_LU", "NL"), ("DE_LU", "AT"), ("DE_LU", "DK_1"), ("DE_LU", "DK_2"),
                   ("DE_LU", "PL"), ("DE_LU", "CZ")]
ALL_BORDERS = BORDERS + DE_REST_BORDERS

T_LOAD, T_GEN, T_FLOW, T_NTC = "entsoe_load", "entsoe_generation", "entsoe_flows", "entsoe_ntc"
T_PRICE, T_CAP = "entsoe_day_ahead_prices", "entsoe_installed_capacity"


def _client(token: str):
    from entsoe import EntsoePandasClient
    return EntsoePandasClient(api_key=token)


def _year_chunks(start: date, end: date):
    cur = pd.Timestamp(start, tz="UTC")
    stop = pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)
    while cur < stop:
        nxt = min(cur + pd.DateOffset(years=1), stop)
        yield cur, nxt
        cur = nxt


def _long(index, series_key, sub_key, label, values) -> pd.DataFrame:
    """Build the standard long-schema frame from a tz-aware datetime index + values."""
    idx = pd.DatetimeIndex(index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    step = (idx[1] - idx[0]) if len(idx) > 1 else pd.Timedelta(hours=1)
    return pd.DataFrame({
        "ts_utc": idx.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "ts_end_utc": (idx + step).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "series_key": series_key, "sub_key": sub_key, "label": label,
        "value": pd.to_numeric(pd.Series(values).values, errors="coerce"),
    }).dropna(subset=["value"])


def _fetch_retry(client_call, attempts=5):
    """Call the entsoe-py query with backoff on transient server errors (503/5xx/429/timeout)."""
    for i in range(attempts):
        try:
            return client_call()
        except requests.exceptions.HTTPError as exc:
            code = getattr(exc.response, "status_code", None)
            if code in (429, 500, 502, 503, 504) and i < attempts - 1:
                time.sleep(2 ** (i + 1)); continue
            raise
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if i < attempts - 1:
                time.sleep(2 ** (i + 1)); continue
            raise


def _do(engine, client_call, table, source, key, force, build):
    """Fetch (unless cached in ingest_log) → build long df(s) → upsert. Returns rows written.

    Per-chunk failures are logged (status='error'/'nodata') and skipped so the backfill continues; a
    re-run retries anything not marked 'ok'. ENTSO-E returns no-data for zones/series it doesn't publish.
    """
    if not force and already_ingested(engine, source, key):
        return 0
    try:
        raw = _fetch_retry(client_call)
    except Exception as exc:  # noqa: BLE001
        low = str(exc).lower()
        if any(s in low for s in ("no matching data", "nodata")) or "NoMatchingData" in type(exc).__name__:
            log_ingest(engine, source, key, 0, status="nodata")
        else:
            log_ingest(engine, source, key, 0, status="error")
            print(f"    ! {source} {key}: {type(exc).__name__} {str(exc)[:80]}", flush=True)
        return 0
    frames = build(raw)
    total = 0
    for df in frames:
        total += upsert_df(engine, table, df, ["ts_utc", "series_key", "sub_key"])
    log_ingest(engine, source, key, total)
    return total


def ingest_load(engine, client, start: date, end: date, zones=None, force=False) -> int:
    ensure_rte_table(engine, T_LOAD)
    total = 0
    for z, area in (zones or ZONES).items():
        for c0, c1 in _year_chunks(start, end):
            def build(s, z=z):
                s = s.iloc[:, 0] if isinstance(s, pd.DataFrame) else s
                return [_long(s.index, z, "", "load_mw", s.values)]
            total += _do(engine, lambda z=area, c0=c0, c1=c1: client.query_load(z, start=c0, end=c1),
                         T_LOAD, f"entsoe:load:{z}", f"{z}_{c0.date()}", force, build)
    return total


def ingest_generation(engine, client, start: date, end: date, zones=None, force=False) -> int:
    ensure_rte_table(engine, T_GEN)
    total = 0
    for z, area in (zones or ZONES).items():
        for c0, c1 in _year_chunks(start, end):
            def build(df, z=z):
                if isinstance(df, pd.Series):
                    df = df.to_frame()
                frames = []
                for col in df.columns:
                    psr = col[0] if isinstance(col, tuple) else col
                    if isinstance(col, tuple) and len(col) > 1 and "Consumption" in str(col[1]):
                        continue  # keep generation, not the storage-consumption leg
                    frames.append(_long(df.index, z, str(psr), "gen_mw", df[col].values))
                return frames
            total += _do(engine, lambda z=area, c0=c0, c1=c1: client.query_generation(z, start=c0, end=c1),
                         T_GEN, f"entsoe:gen:{z}", f"{z}_{c0.date()}", force, build)
    return total


def ingest_prices(engine, client, start: date, end: date, zones=None, force=False) -> int:
    """Day-ahead prices per zone → entsoe_day_ahead_prices (same schema as prices.py, uniform path)."""
    ensure_rte_table(engine, T_PRICE)
    total = 0
    for z, area in (zones or ZONES).items():
        for c0, c1 in _year_chunks(start, end):
            def build(s, z=z):
                s = s.iloc[:, 0] if isinstance(s, pd.DataFrame) else s
                return [_long(s.index, z, "", "day_ahead_eur_mwh", s.values)]
            total += _do(engine,
                         lambda z=area, c0=c0, c1=c1: client.query_day_ahead_prices(z, start=c0, end=c1),
                         T_PRICE, f"entsoe:price:{z}", f"{z}_{c0.date()}", force, build)
    return total


def ingest_installed_capacity(engine, client, start: date, end: date, zones=None, force=False) -> int:
    """Annual installed generation capacity per zone × PSR type → entsoe_installed_capacity (MW).

    The exact stack-sizing input (vs the p99.9-of-generation proxy). One query per zone covers all years.
    """
    ensure_rte_table(engine, T_CAP)
    total = 0
    for z, area in (zones or ZONES).items():
        def build(df, z=z):
            if isinstance(df, pd.Series):
                df = df.to_frame().T
            frames = []
            for col in df.columns:
                psr = col[0] if isinstance(col, tuple) else col
                frames.append(_long(df.index, z, str(psr), "installed_mw", df[col].values))
            return frames
        total += _do(engine, lambda z=area: client.query_installed_generation_capacity(
            z, start=pd.Timestamp(start, tz="UTC"), end=pd.Timestamp(end, tz="UTC")),
            T_CAP, f"entsoe:cap:{z}", f"{z}_{start}", force, build)
    return total


def ingest_flows(engine, client, start: date, end: date, borders=None, force=False) -> int:
    ensure_rte_table(engine, T_FLOW)
    total = 0
    for a, b in (borders or BORDERS):
        for x, y in ((a, b), (b, a)):
            ax, ay = ALL_ZONES[x], ALL_ZONES[y]      # ALL_ZONES: the 7 + the DE_REST constituents
            for c0, c1 in _year_chunks(start, end):
                def build(s, x=x, y=y):
                    return [_long(s.index, f"{x}>{y}", "", "flow_mw", s.values)]
                total += _do(engine,
                             lambda ax=ax, ay=ay, c0=c0, c1=c1: client.query_crossborder_flows(
                                 ax, ay, start=c0, end=c1),
                             T_FLOW, f"entsoe:flow:{x}>{y}", f"{x}>{y}_{c0.date()}", force, build)
    return total


def ingest_all(settings, start: date, end: date, force: bool = False, do_prices=True,
               do_load=True, do_gen=True, do_flows=True) -> dict:
    """Orchestrate price/load/generation/flows ingestion for the 7 zones over [start, end]."""
    from ..db import get_engine
    token = settings.entsoe_token
    if not token:
        raise RuntimeError("ENTSOE_TOKEN missing in .env")
    engine = get_engine(settings.db_url)
    client = _client(token)
    out = {}
    if do_prices:
        out["prices"] = ingest_prices(engine, client, start, end, force=force)
    if do_load:
        out["load"] = ingest_load(engine, client, start, end, force=force)
    if do_gen:
        out["generation"] = ingest_generation(engine, client, start, end, force=force)
    if do_flows:
        out["flows"] = ingest_flows(engine, client, start, end, force=force)
    return out
