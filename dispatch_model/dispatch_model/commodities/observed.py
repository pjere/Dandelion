"""Observed commodity/ETS price series — the real, dated fuel costs behind every historical SRMC.

Why this exists. Until now the SRMC path priced fuel as *annual level × a generic seasonal shape*
(`CommodityModel.monthly_prices`), with gas assumed to peak in winter. For 2022 — whose crisis peaked in
**August** — that inverts the year: the shape produced ~145 €/MWh_th in January and ~108 in August, against
actual TTF of roughly 80 and 235. Every 2022 SRMC was therefore wrong by about a factor of two, in both
directions, which contaminates the 2022 backtest *and* the step-vii markup (fitted on 2019+2022+2023).

This module stores **dated observations** and is deliberately source-agnostic: a licensed export
(Montel / Bloomberg / Refinitiv) and a free public series coexist in one table, distinguished by `source`
and `granularity`. `resolve.py` then picks the finest available series per commodity, falling back to the
scenario trajectory for projection years where no observation can exist.

Canonical units (the rest of the stack assumes these — see `stacks/costs.py`):
    gas  €/MWh_th (TTF, or a zone hub via `commodities.model.zone_prices`)
    coal €/MWh_th (API2)
    oil  $/bbl    (Brent — converted with the calorific/FX constants in `costs.fuel_eur_mwh_th`)
    co2  €/t      (EUA)
"""
from __future__ import annotations

import pandas as pd

from .model import COMMODITIES

LAYER, DATASET = "reference", "commodity_prices"
COLUMNS = ["date", "commodity", "price", "source", "granularity"]
GRANULARITIES = ("daily", "monthly")

#: unit conversions accepted on ingest -> canonical units. Keyed (commodity, from_unit).
_CONVERT = {
    ("gas", "eur_mwh_th"): 1.0,
    ("gas", "eur_mmbtu"): 1.0 / 0.293071,        # 1 MMBtu = 0.293071 MWh
    ("gas", "usd_mmbtu"): None,                  # needs FX -> caller must convert first
    ("coal", "eur_mwh_th"): 1.0,
    ("coal", "eur_t"): 1.0 / 6.978,              # API2 ~6.978 MWh_th per tonne (6000 kcal/kg)
    ("oil", "usd_bbl"): 1.0,
    ("co2", "eur_t"): 1.0,
}


def empty() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in COLUMNS})


def normalise(df: pd.DataFrame, source: str, granularity: str, unit: dict[str, str] | None = None
              ) -> pd.DataFrame:
    """Coerce a raw [date, commodity, price] frame to the canonical schema and units.

    `unit` maps commodity -> the incoming unit (see `_CONVERT`); omitted commodities are assumed already
    canonical. Raises on an unknown commodity, an unsupported unit, or a non-positive price, because a
    silently mis-scaled fuel price is far more damaging than a hard failure here.
    """
    if granularity not in GRANULARITIES:
        raise ValueError(f"granularity must be one of {GRANULARITIES}, got {granularity!r}")
    out = df.rename(columns={c: c.lower() for c in df.columns}).copy()
    missing = {"date", "commodity", "price"} - set(out.columns)
    if missing:
        raise ValueError(f"missing required column(s): {sorted(missing)}")
    out["commodity"] = out["commodity"].astype(str).str.lower().str.strip()
    bad = sorted(set(out["commodity"]) - set(COMMODITIES))
    if bad:
        raise ValueError(f"unknown commodity/ies {bad}; expected {list(COMMODITIES)}")
    out["date"] = pd.to_datetime(out["date"], utc=True).dt.tz_localize(None).dt.normalize()
    out["price"] = pd.to_numeric(out["price"], errors="coerce")
    for com, frm in (unit or {}).items():
        key = (com, str(frm).lower())
        if key not in _CONVERT:
            raise ValueError(f"unsupported unit {frm!r} for {com!r}; known: "
                             f"{[k[1] for k in _CONVERT if k[0] == com]}")
        factor = _CONVERT[key]
        if factor is None:
            raise ValueError(f"{com} in {frm} needs an FX conversion before ingest (no FX series here)")
        out.loc[out["commodity"] == com, "price"] *= factor
    out = out.dropna(subset=["date", "price"])
    if (out["price"] <= 0).any():
        raise ValueError("non-positive prices after conversion — check units/sign")
    out["source"], out["granularity"] = str(source), granularity
    return (out[COLUMNS].drop_duplicates(["date", "commodity", "source"])
            .sort_values(["commodity", "date"]).reset_index(drop=True))


def ingest_csv(path, source: str, granularity: str, unit: dict[str, str] | None = None,
               write: bool = True) -> pd.DataFrame:
    """Ingest a licensed/vendor CSV of [date, commodity, price] into the observed store.

    The file stays outside the repo (it is usually licensed); only the normalised series is stored.
    Example: `ingest_csv("ttf_daily.csv", source="montel", granularity="daily")`.
    """
    raw = pd.read_csv(path)
    df = normalise(raw, source=source, granularity=granularity, unit=unit)
    if write:
        write_observed(df, source=source)
    return df


def write_observed(df: pd.DataFrame, source: str):
    """Persist one source's slice (partitioned by source, so sources can be refreshed independently)."""
    from powersim_core import lake
    return lake.write_table(df, LAYER, DATASET, index=False, source=source)


def read_observed(source: str | None = None) -> pd.DataFrame:
    """All observed series, or one source's. Empty frame when nothing has been ingested yet."""
    from powersim_core import lake
    try:
        df = lake.read_table(LAYER, DATASET, source=source) if source else lake.read_table(LAYER, DATASET)
    except (FileNotFoundError, ValueError, KeyError):
        return empty()
    return df if df is not None and not df.empty else empty()


def coverage(df: pd.DataFrame | None = None) -> pd.DataFrame:
    """What is actually available: rows, span and granularity per (commodity, source) — the honest
    inventory to consult before trusting a historical SRMC."""
    d = read_observed() if df is None else df
    if d.empty:
        return pd.DataFrame(columns=["commodity", "source", "granularity", "n", "start", "end"])
    d = d.copy()
    d["date"] = pd.to_datetime(d["date"])
    g = d.groupby(["commodity", "source", "granularity"])["date"]
    return (pd.DataFrame({"n": g.size(), "start": g.min(), "end": g.max()})
            .reset_index().sort_values(["commodity", "granularity", "source"]))
