"""Normalisation des réponses JSON RTE vers le schéma long commun, et extraction complète.

Schéma cible (une table par ressource) :
    ts_utc, ts_end_utc, series_key, sub_key, label, value

L'API renvoie en général : { <racine> : [ { <champs d'identité...>, "values": [ {start_date,
end_date, value}, ... ] }, ... ] }. On localise la racine (unique clé dont la valeur est une
liste), puis pour chaque élément on dérive (series_key, sub_key, label) via une fonction
d'identité par ressource (avec repli générique), et on déplie ``values``.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from sqlalchemy.engine import Engine

from ..config import RteResource
from ..db import already_ingested, ensure_rte_table, log_ingest, upsert_df
from .client import RteClient, chunk_key, iter_chunks, iter_year_chunks

# ----- Fonctions d'identité par ressource ----------------------------------

def _unit_eic(it: dict) -> str:
    unit = it.get("unit")
    if isinstance(unit, dict):
        return str(unit.get("eic_code") or unit.get("name") or "")
    return str(it.get("eic_code") or it.get("eic_code_pole") or it.get("name") or "")


def _unit_name(it: dict) -> str:
    unit = it.get("unit")
    if isinstance(unit, dict):
        return str(unit.get("name") or "")
    return str(it.get("name") or "")


def _unit_fuel(it: dict) -> str:
    unit = it.get("unit")
    if isinstance(unit, dict):
        return str(unit.get("production_type") or "")
    return str(it.get("production_type") or "")


def _flows_key(it: dict) -> str:
    snd = it.get("sender_country_name") or it.get("sender_country_eic_code") or it.get("sender") or ""
    rcv = it.get("receiver_country_name") or it.get("receiver_country_eic_code") or it.get("receiver") or ""
    return f"{snd}->{rcv}"


IdentityFn = Callable[[dict], tuple[str, str, str]]  # -> (series_key, sub_key, label)

IDENTITY: dict[str, IdentityFn] = {
    "generation_per_type": lambda it: (it.get("production_type", ""), "", ""),
    "generation_per_unit": lambda it: (_unit_eic(it), _unit_fuel(it), _unit_name(it)),
    "water_reserves": lambda it: ("water_reserve", "", ""),
    "generation_mix_15min": lambda it: (it.get("production_type", "mix"), str(it.get("production_subtype", "")), ""),
    "generation_forecast": lambda it: (it.get("production_type", "") or str(it.get("type", "")),
                                       str(it.get("type", "")), ""),
    "installed_capacities": lambda it: (it.get("production_type") or str(it.get("type", "")), "", ""),
    "installed_capacities_per_unit": lambda it: (_unit_eic(it), _unit_fuel(it), _unit_name(it)),
    "consumption_short_term": lambda it: (str(it.get("type", "REALISED")), "", ""),
    "consumption_weekly_forecast": lambda it: (str(it.get("type", "FORECAST")), "", ""),
    "physical_flows": lambda it: (_flows_key(it), "", ""),
    "commercial_exchanges": lambda it: (_flows_key(it), "", ""),
    "wholesale_market_prices": lambda it: (str(it.get("price_type", it.get("type", "price"))), "", ""),
}


def _generic_identity(it: dict) -> tuple[str, str, str]:
    """Repli : concatène les champs string non temporels comme clé de série."""
    skip = {"values", "value", "start_date", "end_date", "updated_date"}
    parts = [f"{k}={v}" for k, v in it.items() if k not in skip and isinstance(v, (str, int, float))]
    return ("|".join(parts) if parts else "series", "", "")


def _to_utc_iso(value: str | None) -> str | None:
    """Convertit une date ISO RTE (avec offset, ex. +01:00) en ISO UTC normalisé."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value  # on conserve tel quel si format inattendu
    if dt.tzinfo is None:
        # supposé déjà UTC si pas d'offset
        return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _find_root_list(payload: dict) -> list[dict]:
    """Localise les éléments racine de la réponse RTE.

    Selon la ressource, la valeur sous la clé racine est soit une **liste** d'éléments
    (cas usuel), soit un **dict** unique (ex. water_reserves) -> on l'enveloppe dans une liste.
    """
    if not isinstance(payload, dict):
        return []
    for value in payload.values():
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            return [value]
    return []


def normalize(res: RteResource, payload: dict) -> pd.DataFrame:
    """Transforme un JSON RTE en DataFrame au schéma long standard."""
    items = _find_root_list(payload)
    identity = IDENTITY.get(res.name, _generic_identity)
    rows: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        item_fields = {k: val for k, val in it.items() if k != "values"}
        values = it.get("values")
        if values is None and "value" in it:
            values = [it]  # élément déjà « plat »
        for v in values or []:
            # Contexte fusionné : la dimension de série peut se trouver au niveau de
            # l'élément OU de la valeur (ex. installed_capacities -> 'type' dans la valeur).
            ctx = {**item_fields, **v} if isinstance(v, dict) else item_fields
            try:
                series_key, sub_key, label = identity(ctx)
            except Exception:
                series_key, sub_key, label = _generic_identity(ctx)
            rows.append(
                {
                    "ts_utc": _to_utc_iso(v.get("start_date")),
                    "ts_end_utc": _to_utc_iso(v.get("end_date")),
                    "series_key": str(series_key or ""),
                    "sub_key": str(sub_key or ""),
                    "label": str(label or ""),
                    "value": v.get("value"),
                }
            )
    df = pd.DataFrame(rows, columns=["ts_utc", "ts_end_utc", "series_key", "sub_key", "label", "value"])
    df = df[df["ts_utc"].notna()]
    return df.drop_duplicates(subset=["ts_utc", "series_key", "sub_key"], keep="last")


def extract_resource(
    engine: Engine,
    client: RteClient,
    res: RteResource,
    period_start,
    period_end,
    force: bool = False,
) -> int:
    """Extrait une ressource sur toute la période (chunkée + cachée + incrémentale)."""
    ensure_rte_table(engine, res.table)

    # Ressources « snapshot » (sans paramètre de date) : un seul appel renvoyant la dernière
    # publication (ex. prix Spot france_power_exchanges). On capture l'instantané du jour.
    if res.params == "none":
        from datetime import date as _date

        payload = client.fetch_chunk(res, None, None, use_cache=False)  # type: ignore[arg-type]
        df = normalize(res, payload)
        rows = upsert_df(engine, res.table, df, ["ts_utc", "series_key", "sub_key"])
        log_ingest(engine, f"rte:{res.name}", f"snapshot_{_date.today().isoformat()}", rows)
        return rows

    start = max(res.start_date, period_start)
    if start > period_end:
        return 0
    total = 0
    source = f"rte:{res.name}"
    chunks = (
        iter_year_chunks(start, period_end)
        if res.params == "yearly"
        else iter_chunks(start, period_end, res.chunk_days)
    )
    for c0, c1 in chunks:
        key = chunk_key(c0, c1)
        is_last = c1.date() >= period_end  # dernier chunk = données encore évolutives
        if not force and not is_last and already_ingested(engine, source, key):
            continue
        payload = client.fetch_chunk(res, c0, c1)
        df = normalize(res, payload)
        rows = upsert_df(engine, res.table, df, ["ts_utc", "series_key", "sub_key"])
        log_ingest(engine, source, key, rows)
        total += rows
    return total
