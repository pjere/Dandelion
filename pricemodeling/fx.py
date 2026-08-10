"""ECB daily reference exchange rates — the FX series the lake needs to hold one currency.

Every price in the lake is EUR/MWh. GB is the exception that forces the issue: Elexon quotes GBP/MWh, so
`elexon.series.ingest_prices` REFUSES to write without a rate rather than let a currency mix reach the
lake, where it would corrupt every cross-zone comparison and the markup fit. This supplies it.

Source: ECB Statistical Data Warehouse, series `EXR.D.<CCY>.EUR.SP00.A` — public, no key, CSV. The
quoted value is <CCY> PER EUR (GBP/EUR 0.85175 on 2024-06-03), so a GBP price becomes EUR by DIVIDING.

The ECB publishes on TARGET business days only, so weekends and holidays are forward-filled: an FX rate
is a price level that persists until the next quote, unlike a flow that is genuinely zero when absent.

Also relevant beyond GB: `stacks.costs` carries a hardcoded `_USD_PER_EUR = 1.08` for the oil
conversion. That is a constant standing in for exactly this series and could be replaced by it.
"""
from __future__ import annotations

import io
from datetime import date

import pandas as pd
import requests

from .db import ensure_rte_table, upsert_df

T_FX = "ecb_fx"
BASE = "https://data-api.ecb.europa.eu/service/data/EXR"


def fetch_ecb_rate(currency: str, start: date, end: date, attempts: int = 4) -> pd.Series:
    """→ Series of <currency> per EUR, indexed by date, business days only."""
    url = f"{BASE}/D.{currency.upper()}.EUR.SP00.A"
    params = {"startPeriod": str(start), "endPeriod": str(end), "format": "csvdata"}
    last = None
    for i in range(attempts):
        try:
            r = requests.get(url, params=params, timeout=120)
            if r.status_code in (429, 500, 502, 503, 504) and i < attempts - 1:
                import time
                time.sleep(2 ** (i + 1)); continue
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text))
            s = (df.assign(d=pd.to_datetime(df["TIME_PERIOD"]))
                   .set_index("d")["OBS_VALUE"].astype(float).sort_index())
            return s[~s.index.duplicated()]
        except Exception as exc:                                          # noqa: BLE001
            last = exc
            if i == attempts - 1:
                raise
    raise RuntimeError(f"ECB fetch failed: {last}")


def ingest_fx(engine, start: date, end: date, currencies=("GBP",), force: bool = False) -> int:
    """Fetch and upsert ECB daily rates into `ecb_fx` (long schema, series_key = currency)."""
    from .entsoe.series import _long
    ensure_rte_table(engine, T_FX)
    total = 0
    for ccy in currencies:
        s = fetch_ecb_rate(ccy, start, end)
        if s.empty:
            continue
        idx = pd.DatetimeIndex(s.index).tz_localize("UTC")
        df = _long(idx, ccy.upper(), "", "per_eur", s.values)
        total += upsert_df(engine, T_FX, df, ["ts_utc", "series_key", "sub_key"])
    return total


def load_fx(config, currency: str = "GBP") -> pd.Series:
    """→ daily <currency>-per-EUR from the lake, forward-filled to a continuous daily index.

    Forward fill is correct here and would not be for a flow: an exchange rate is a level that holds
    until the next quote, whereas an absent flow is genuinely zero. Without it every weekend hour of GB
    price data would be dropped."""
    import sqlite3
    con = sqlite3.connect(config.resolve(config.section("data")["sqlite_path"]))
    try:
        df = pd.read_sql("SELECT ts_utc, value FROM ecb_fx WHERE series_key = ?",
                         con, params=(currency.upper(),))
    finally:
        con.close()
    if df.empty:
        return pd.Series(dtype=float)
    s = (df.assign(d=pd.to_datetime(df["ts_utc"], utc=True).dt.date)
           .groupby("d")["value"].mean().sort_index())
    full = pd.date_range(min(s.index), max(s.index), freq="D").date
    return s.reindex(full).ffill()


def rate_fn(config, currency: str = "GBP"):
    """→ callable date -> rate, for `elexon.series.ingest_prices(gbp_per_eur=...)`.

    Raises on an empty series rather than returning a default: silently defaulting an FX rate is exactly
    the failure `ingest_prices` refuses to allow."""
    s = load_fx(config, currency)
    if s.empty:
        raise RuntimeError(f"no {currency} rate in `{T_FX}` — run `ingest_fx` first")
    lo, hi = min(s.index), max(s.index)

    def _f(d):
        d = d if not hasattr(d, "date") else d.date()
        return float(s.get(d, s.loc[lo] if d < lo else s.loc[hi]))
    return _f
