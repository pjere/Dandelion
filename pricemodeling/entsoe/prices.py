"""Prix day-ahead (Spot) depuis ENTSO-E Transparency -> table entsoe_day_ahead_prices.

API : GET https://web-api.tp.entsoe.eu/api
      ?securityToken=...&documentType=A44&in_Domain=ZONE&out_Domain=ZONE
      &periodStart=yyyyMMddHHmm&periodEnd=yyyyMMddHHmm   (UTC, max 1 an/appel)
Réponse XML : Publication_MarketDocument > TimeSeries > Period > Point(position, price.amount).
Stocké au schéma long commun (series_key = zone, value = €/MWh) pour s'intégrer à la fusion.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..db import already_ingested, ensure_rte_table, log_ingest, upsert_df

API_URL = "https://web-api.tp.entsoe.eu/api"
TABLE = "entsoe_day_ahead_prices"

_RES_DELTA = {
    "PT60M": timedelta(hours=1),
    "PT30M": timedelta(minutes=30),
    "PT15M": timedelta(minutes=15),
}


class EntsoeError(RuntimeError):
    pass


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H%M")


def iter_year_chunks(start: date, end: date):
    """Fenêtres <= 1 an (UTC), bornes [c0, c1)."""
    cur = datetime(start.year, start.month, start.day, tzinfo=UTC)
    end_dt = datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1)
    while cur < end_dt:
        nxt = min(cur + timedelta(days=365), end_dt)
        yield cur, nxt
        cur = nxt


@retry(
    retry=retry_if_exception_type(requests.RequestException),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _get(params: dict) -> str:
    resp = requests.get(API_URL, params=params, timeout=120)
    if resp.status_code == 429:
        raise requests.RequestException("429 Too Many Requests")
    if resp.status_code >= 500:
        raise requests.RequestException(f"{resp.status_code} serveur ENTSO-E")
    if resp.status_code == 401:
        raise EntsoeError("Token ENTSO-E invalide ou accès API non activé (401).")
    resp.raise_for_status()
    return resp.text


def parse_prices(xml_text: str, zone: str) -> pd.DataFrame:
    """Parse un Publication_MarketDocument A44 en DataFrame long."""
    root = ET.fromstring(xml_text)
    rootname = _strip_ns(root.tag)
    if rootname.startswith("Acknowledgement"):
        # ex. "No matching data found" -> pas une erreur fatale
        return pd.DataFrame(columns=["ts_utc", "ts_end_utc", "series_key", "sub_key", "label", "value"])

    rows = []
    for ts in root.iter():
        if _strip_ns(ts.tag) != "TimeSeries":
            continue
        for period in ts:
            if _strip_ns(period.tag) != "Period":
                continue
            start_txt = resolution = None
            points = []
            for child in period:
                name = _strip_ns(child.tag)
                if name == "timeInterval":
                    for ti in child:
                        if _strip_ns(ti.tag) == "start":
                            start_txt = ti.text
                elif name == "resolution":
                    resolution = child.text
                elif name == "Point":
                    pos = amount = None
                    for pc in child:
                        pn = _strip_ns(pc.tag)
                        if pn == "position":
                            pos = int(pc.text)
                        elif pn in ("price.amount", "price_amount", "amount"):
                            amount = float(pc.text)
                    if pos is not None and amount is not None:
                        points.append((pos, amount))
            if not start_txt or resolution not in _RES_DELTA:
                continue
            start_dt = datetime.fromisoformat(start_txt.replace("Z", "+00:00"))
            delta = _RES_DELTA[resolution]
            for pos, amount in points:
                ts0 = start_dt + (pos - 1) * delta
                rows.append({
                    "ts_utc": ts0.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                    "ts_end_utc": (ts0 + delta).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                    "series_key": zone,
                    "sub_key": "",
                    "label": "day_ahead_eur_mwh",
                    "value": amount,
                })
    df = pd.DataFrame(rows, columns=["ts_utc", "ts_end_utc", "series_key", "sub_key", "label", "value"])
    return df.drop_duplicates(subset=["ts_utc", "series_key", "sub_key"], keep="last")


def extract_prices(
    engine, token: str, raw_dir: Path, zone: str, start: date, end: date, force: bool = False
) -> int:
    """Extrait les prix day-ahead sur la période (chunks annuels, cache XML)."""
    if not token:
        raise EntsoeError(
            "Token ENTSO-E manquant. Renseignez ENTSOE_TOKEN dans .env (cf. .env.example)."
        )
    ensure_rte_table(engine, TABLE)
    cache_dir = raw_dir / "entsoe" / "day_ahead"
    cache_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    source = "entsoe:day_ahead"
    for c0, c1 in iter_year_chunks(start, end):
        key = f"{c0.date().isoformat()}_{c1.date().isoformat()}"
        is_last = c1.date() >= end
        if not force and not is_last and already_ingested(engine, source, key):
            continue
        cache = cache_dir / f"{zone}_{key}.xml"
        if cache.exists() and cache.stat().st_size > 0 and not force:
            xml_text = cache.read_text(encoding="utf-8")
        else:
            params = {
                "securityToken": token,
                "documentType": "A44",
                "in_Domain": zone,
                "out_Domain": zone,
                "periodStart": _fmt(c0),
                "periodEnd": _fmt(c1),
            }
            xml_text = _get(params)
            cache.write_text(xml_text, encoding="utf-8")
        df = parse_prices(xml_text, zone)
        rows = upsert_df(engine, TABLE, df, ["ts_utc", "series_key", "sub_key"])
        log_ingest(engine, source, key, rows)
        total += rows
    return total
