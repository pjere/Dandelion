"""Réconciliation des groupes de production : construit dim_production_unit.

Stratégie (du plus fiable au moins fiable) :
1. Clé primaire = **code EIC** (``series_key`` de rte_generation_per_unit), stable entre années.
   On regroupe tous les libellés observés comme alias.
2. Overrides manuels (unit_overrides.yaml) : fusion d'EIC, nom canonique forcé, rattachement
   d'un libellé sans EIC à un EIC.
3. Pour les entrées sans EIC exploitable : fuzzy matching du libellé sur les noms canoniques
   des groupes à EIC fiable (rapidfuzz), au-dessus d'un seuil.
4. Les cas non résolus automatiquement sont écrits dans un rapport pour revue humaine.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
import yaml
from rapidfuzz import fuzz, process
from sqlalchemy import text
from sqlalchemy.engine import Engine

from ..db import upsert_df

OVERRIDES_PATH = Path(__file__).with_name("unit_overrides.yaml")
PER_UNIT_TABLE = "rte_generation_per_unit"


def _load_overrides() -> dict:
    if not OVERRIDES_PATH.exists():
        return {}
    with OVERRIDES_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _is_real_eic(eic: str) -> bool:
    """Heuristique : un vrai code EIC est non vide, long et sans espace."""
    return bool(eic) and len(eic) >= 12 and " " not in eic


def _norm(name: str) -> str:
    """Normalise un libellé pour la comparaison (minuscule, sans ponctuation ni espace).

    Neutralise les écarts typographiques inter-années : « CHOOZ B 1 », « CHOOZ B-1 »,
    « Chooz B1 » -> « choozb1 ».
    """
    return re.sub(r"[^0-9a-z]", "", str(name).lower())


def _read_unit_observations(engine: Engine) -> pd.DataFrame:
    sql = text(
        f"""
        SELECT series_key AS eic, label AS name, sub_key AS fuel,
               COUNT(*) AS n, MIN(ts_utc) AS first_seen, MAX(ts_utc) AS last_seen
        FROM "{PER_UNIT_TABLE}"
        GROUP BY series_key, label, sub_key
        """
    )
    with engine.connect() as conn:
        return pd.read_sql(sql, conn)


def reconcile_units(engine: Engine, report_path: Path) -> dict:
    """Construit dim_production_unit et écrit un rapport. Retourne des statistiques."""
    obs = _read_unit_observations(engine)
    if obs.empty:
        return {"units": 0, "unmatched": 0, "note": f"table {PER_UNIT_TABLE} vide"}

    overrides = _load_overrides()
    eic_merge: dict[str, str] = overrides.get("eic_merge") or {}
    forced_names: dict[str, str] = overrides.get("canonical_names") or {}
    name_to_eic: dict[str, str] = overrides.get("name_to_eic") or {}
    threshold: int = int(overrides.get("fuzzy_threshold", 92))

    # --- Agrégation par EIC observé ---
    units: dict[str, dict] = {}
    for eic, grp in obs.groupby("eic"):
        eic = str(eic)
        names = grp.sort_values("n", ascending=False)
        aliases = sorted({str(x) for x in names["name"] if str(x).strip()})
        canonical_name = forced_names.get(eic) or (str(names.iloc[0]["name"]) if len(names) else "")
        fuel = (
            grp.groupby("fuel")["n"].sum().sort_values(ascending=False).index[0]
            if grp["fuel"].notna().any()
            else ""
        )
        units[eic] = {
            "eic_code": eic,
            "canonical_eic": eic_merge.get(eic, eic),
            "canonical_name": canonical_name,
            "fuel_type": str(fuel or ""),
            "aliases": json.dumps(aliases, ensure_ascii=False),
            "n_obs": int(grp["n"].sum()),
            "first_seen": str(grp["first_seen"].min()),
            "last_seen": str(grp["last_seen"].max()),
            "match_source": "override" if eic in eic_merge or eic in forced_names else "eic",
        }

    # Référentiel des groupes à EIC fiable (cibles du fuzzy matching).
    # Indexé sur le nom NORMALISÉ pour neutraliser ponctuation/espaces entre années.
    real_units = {e: u for e, u in units.items() if _is_real_eic(e)}
    name_index = {_norm(u["canonical_name"]): e for e, u in real_units.items() if u["canonical_name"]}
    choices = list(name_index.keys())

    # --- Résolution des entrées sans EIC fiable ---
    unmatched: list[dict] = []
    for eic, u in units.items():
        if _is_real_eic(eic):
            continue
        # 1) override explicite nom -> eic
        target = name_to_eic.get(u["canonical_name"]) or name_to_eic.get(eic)
        if target:
            u["canonical_eic"] = target
            u["match_source"] = "override"
            continue
        # 2) fuzzy matching sur les noms canoniques fiables (comparaison normalisée)
        best = process.extractOne(_norm(u["canonical_name"]), choices, scorer=fuzz.ratio) if choices else None
        if best and best[1] >= threshold:
            u["canonical_eic"] = name_index[best[0]]
            u["match_source"] = f"fuzzy:{int(best[1])}"
        else:
            u["match_source"] = "unmatched"
            unmatched.append({**u, "best_match": best[0] if best else "", "score": best[1] if best else 0})

    df = pd.DataFrame(list(units.values()))
    upsert_df(engine, "dim_production_unit", df, ["eic_code"])

    # --- Rapport ---
    report_path.parent.mkdir(parents=True, exist_ok=True)
    df_report = df.sort_values(["match_source", "canonical_name"])
    df_report.to_csv(report_path, index=False, encoding="utf-8")
    if unmatched:
        unmatched_path = report_path.with_name("reconciliation_unmatched.csv")
        pd.DataFrame(unmatched).to_csv(unmatched_path, index=False, encoding="utf-8")

    return {
        "units": len(units),
        "real_eic": len(real_units),
        "unmatched": len(unmatched),
        "report": str(report_path),
    }
