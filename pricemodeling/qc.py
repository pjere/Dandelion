"""Contrôle de cohérence entre les sources de production : RTE vs ENTSO-E.

Pourquoi ce module existe : le trou de publication RTE de sept/oct 2024 (nucléaire nul ou déprimé pendant
quatre semaines) n'a été détecté que parce qu'il faisait sortir le dispatch à +190 % d'erreur baseload.
Rien dans l'ETL ne le signalait. Le repli de `merge.build_master` corrige désormais le symptôme
automatiquement — ce qui crée un risque nouveau : un défaut de source devient **invisible** parce qu'il est
réparé en silence. D'où ce contrôle, qui rend la divergence observable indépendamment de sa correction.

Deux différences importantes avec le repli :

1. **Il est symétrique.** Le repli ne se déclenche que si RTE est trop bas, puisque c'est la seule
   direction qu'il sait réparer. Une série RTE anormalement *haute* passerait inaperçue ; ici les deux
   sens sont signalés.
2. **Il ne corrige rien.** Il constate, et distingue les divergences déjà connues et expliquées de celles
   qui ne le sont pas — seules ces dernières méritent qu'on aille voir.

Le pompage est exclu : RTE le publie net de pompage, l'ingestion ENTSO-E écarte la jambe consommation.
Les deux divergent dans tous les mois par construction ; ce n'est pas un défaut.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from .merge.build_master import ENTSOE_FALLBACK

FLOOR_MW = 200.0        # sous ce niveau, l'écart relatif n'est pas significatif
REL_TOL = 0.20          # au-delà de 20 % d'écart mensuel, on veut savoir pourquoi
MIN_HOURS = 24          # un mois n'est signalé qu'à partir d'une journée d'heures touchées


@dataclass(frozen=True)
class KnownIncident:
    """Divergence déjà instruite : on l'attend, elle ne doit plus déclencher d'alerte."""
    months: tuple[str, ...]
    reason: str


#: Divergences constatées, expliquées et traitées. Y ajouter une entrée plutôt que d'assouplir les seuils.
KNOWN_INCIDENTS = (
    KnownIncident(
        months=("2024-09", "2024-10"),
        reason="Trou de publication RTE du 16/09 au 13/10/2024 (nucléaire nul 149 h, déprimé 552 h). "
               "Confirmé en re-téléchargeant : RTE renvoie ces valeurs à l'identique. Corrigé par le "
               "repli ENTSO-E de build_master ; signalé à RTE.",
    ),
)
_KNOWN_MONTHS = {m for inc in KNOWN_INCIDENTS for m in inc.months}


def _entsoe_fr_monthly(engine: Engine) -> pd.DataFrame:
    with engine.connect() as conn:
        df = pd.read_sql(
            text("SELECT substr(ts_utc,1,7) month, sub_key, AVG(value) v, COUNT(*) n "
                 "FROM entsoe_generation WHERE series_key='FR' GROUP BY month, sub_key"), conn)
    inv = {v: k for k, v in ENTSOE_FALLBACK.items()}
    df = df[df["sub_key"].isin(inv)].copy()
    df["column"] = df["sub_key"].map(inv)
    return df[["month", "column", "v"]].rename(columns={"v": "entsoe"})


def _rte_monthly(engine: Engine) -> pd.DataFrame:
    with engine.connect() as conn:
        df = pd.read_sql(
            text("SELECT substr(ts_utc,1,7) month, series_key, AVG(value) v, "
                 "SUM(CASE WHEN value=0 THEN 1 ELSE 0 END) n_zero, COUNT(*) n "
                 "FROM rte_generation_per_type GROUP BY month, series_key"), conn)
    # series_key porte le type de production RTE ("NUCLEAR", "WIND_ONSHORE", …) → colonne prod_*
    df["column"] = "prod_" + df["series_key"].str.lower().str.replace(r"[^0-9a-z]+", "_", regex=True)
    return df[["month", "column", "v", "n_zero", "n"]].rename(columns={"v": "rte"})


def source_divergence(engine: Engine) -> pd.DataFrame:
    """Écart mensuel RTE↔ENTSO-E par filière. Une ligne par (mois, colonne) comparable."""
    if not all(n in inspect(engine).get_table_names()
               for n in ("rte_generation_per_type", "entsoe_generation")):
        return pd.DataFrame()
    d = _rte_monthly(engine).merge(_entsoe_fr_monthly(engine), on=["month", "column"], how="inner")
    if d.empty:
        return d
    ref = d[["rte", "entsoe"]].abs().max(axis=1)
    d["diff"] = d["rte"] - d["entsoe"]
    d["rel"] = d["diff"] / ref.where(ref > 0)
    d["material"] = (ref >= FLOOR_MW) & (d["rel"].abs() > REL_TOL) & (d["n"] >= MIN_HOURS)
    d["known"] = d["month"].isin(_KNOWN_MONTHS)
    d["direction"] = pd.Series(["rte_bas"] * len(d)).where(d["diff"] < 0, "rte_haut").to_numpy()
    return d.sort_values(["month", "column"]).reset_index(drop=True)


def unexplained(d: pd.DataFrame) -> pd.DataFrame:
    """Divergences matérielles qui ne relèvent d'aucun incident connu — le seul sous-ensemble actionnable."""
    if d.empty:
        return d
    return d[d["material"] & ~d["known"]].sort_values("rel", key=abs, ascending=False)


def report(engine: Engine) -> tuple[str, int]:
    """Rapport lisible + nombre de divergences inexpliquées (0 = rien à instruire)."""
    d = source_divergence(engine)
    if d.empty:
        return "QC sources : aucune donnée comparable (tables RTE/ENTSO-E absentes ou vides).", 0
    u = unexplained(d)
    lines = [f"QC sources RTE vs ENTSO-E — {d['month'].nunique()} mois × {d['column'].nunique()} filières",
             f"  seuils : |écart| > {REL_TOL:.0%}, niveau ≥ {FLOOR_MW:.0f} MW, ≥ {MIN_HOURS} h/mois",
             "  pompage exclu (RTE net / ENTSO-E brut : divergence de convention, pas un défaut)", ""]
    known = d[d["material"] & d["known"]]
    if len(known):
        lines.append(f"Incidents connus, déjà traités : {len(known)} cellules")
        for inc in KNOWN_INCIDENTS:
            lines.append(f"  - {', '.join(inc.months)} : {inc.reason}")
        lines.append("")
    if u.empty:
        lines.append("Aucune divergence inexpliquée.")
    else:
        lines.append(f"DIVERGENCES INEXPLIQUÉES : {len(u)} — à instruire")
        cols = ["month", "column", "rte", "entsoe", "diff", "rel", "n_zero", "direction"]
        lines.append(u[cols].round(2).to_string(index=False))
    return "\n".join(lines), len(u)
