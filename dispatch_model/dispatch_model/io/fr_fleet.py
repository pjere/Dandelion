"""FR dispatchable fleet reader (DB) — unit id / technology / capacity for the unit-level stack.

Self-contained (no cross-package import of availability_model): capacity = p99.9 of per-unit production
(robust to data spikes, same rationale as step v). Availability over time is injected at solve time
(historical actuals for backtest, step-v draws for projection).

**Deux corrections structurelles (`year` renseigné).** Le stack était auparavant le parc *maximum
historique*, et non le parc de l'année modélisée :

1. *Unités périmées.* Le scan de capacité porte sur tout l'historique sans filtre de déclassement, donc
   des centrales fermées depuis dix ans restaient au stack : Vitry 4, Bouchain 1, La Maxe 1 (charbon,
   fermées en 2015), Aramon 1-2 (2016), Porcheville 1-4 (2017-18) — et à l'inverse Flamanville 3, couplée
   en décembre 2024, était présente dès 2019. D'où +156 % de charbon et +134 % de fioul face au parc réel.
   `active_units` ne retient que les unités ayant réellement produit dans l'année.

2. *Capacité manquante.* Seules les unités déclarées groupe par groupe par RTE entrent au stack, ce qui
   exclut tout le parc diffus : hydraulique de lac -75 % (2 140 MW contre 8 702 installés), gaz -33 %,
   biomasse -88 %. `installed_by_tech` fournit l'installé RTE de l'année, et l'écart est ajouté en bloc
   agrégé par `build_fr_stack`.

Sans `year`, le comportement historique est conservé — aucun appelant existant ne change de résultat par
accident.
"""
from __future__ import annotations

import sqlite3

import pandas as pd

from ..config import Config

_FUEL2TECH = {
    "NUCLEAR": "nuclear", "FOSSIL_GAS": "gas", "FOSSIL_HARD_COAL": "coal", "FOSSIL_OIL": "oil",
    "BIOMASS": "biomass", "HYDRO_WATER_RESERVOIR": "hydro_reservoir",
    "HYDRO_PUMPED_STORAGE": "hydro_psp", "HYDRO_RUN_OF_RIVER_AND_POUNDAGE": "hydro_ror",
}


#: en deçà, l'écart installé/déclaré n'est pas un parc manquant mais du bruit de réconciliation
MIN_TOPUP_MW = 100.0
#: une unité est considérée active si elle a produit au moins ce niveau dans l'année
ACTIVE_MIN_MW = 1.0


def latest_fleet_year(config: Config) -> int | None:
    """Dernière année où le parc est observé groupe par groupe.

    Base de parc des projections : l'année *météo* de référence ne convient pas (une projection 2046 sur
    une météo 2019 hériterait du parc 2019, Fessenheim comprise). Les trajectoires TYNDP font ensuite
    évoluer ce parc réel le plus récent.
    """
    con = sqlite3.connect(config.resolve(config.section("data")["sqlite_path"]))
    try:
        y = pd.read_sql("SELECT MAX(substr(ts_utc,1,4)) y FROM rte_generation_per_unit", con)["y"].iloc[0]
    finally:
        con.close()
    return int(y) if y else None


def active_units(config: Config, year: int) -> set[str]:
    """EIC des unités ayant réellement produit dans `year` (vide si l'année n'est pas couverte)."""
    con = sqlite3.connect(config.resolve(config.section("data")["sqlite_path"]))
    try:
        df = pd.read_sql(
            "SELECT DISTINCT series_key FROM rte_generation_per_unit "
            "WHERE substr(ts_utc,1,4)=? AND value > ?", con, params=(str(year), ACTIVE_MIN_MW))
    finally:
        con.close()
    return set(df["series_key"])


def installed_by_tech(config: Config, year: int) -> dict[str, float]:
    """Capacité installée RTE par technologie pour `year` (dernière année disponible si postérieure)."""
    con = sqlite3.connect(config.resolve(config.section("data")["sqlite_path"]))
    try:
        df = pd.read_sql(
            "SELECT series_key, substr(ts_utc,1,4) y, MAX(value) v "
            "FROM rte_installed_capacities_per_type GROUP BY series_key, y", con)
    finally:
        con.close()
    if df.empty:
        return {}
    df["y"] = pd.to_numeric(df["y"], errors="coerce")
    df = df.dropna(subset=["y"])
    # une année de projection n'a pas d'installé publié : on retient la dernière connue, que les
    # trajectoires TYNDP feront ensuite évoluer
    yr = min(int(year), int(df["y"].max()))
    sel = df[df["y"] == yr]
    out: dict[str, float] = {}
    for _, r in sel.iterrows():
        tech = _FUEL2TECH.get(str(r["series_key"]))
        if tech:
            out[tech] = out.get(tech, 0.0) + float(r["v"])
    return out


def load_fr_fleet(config: Config, year: int | None = None) -> pd.DataFrame:
    """→ [unit_id, name, tech, capacity_mw] for FR dispatchable units. Capacity scan is disk-cached."""
    from .cache import cached, db_key
    d = config.section("data")
    con = sqlite3.connect(config.resolve(d["sqlite_path"]))
    try:
        reg = pd.read_sql("SELECT eic_code, canonical_name, fuel_type FROM dim_production_unit", con)
    finally:
        con.close()

    def _cap() -> pd.DataFrame:
        con2 = sqlite3.connect(config.resolve(d["sqlite_path"]))
        try:
            return pd.read_sql(
                "SELECT eic AS unit_id, value AS cap FROM ("
                "  SELECT series_key AS eic, value,"
                "         ROW_NUMBER() OVER (PARTITION BY series_key ORDER BY value) AS rn,"
                "         COUNT(*)     OVER (PARTITION BY series_key)                AS n"
                "  FROM rte_generation_per_unit WHERE value IS NOT NULL"
                ") WHERE rn = MAX(1, CAST(0.999 * n AS INTEGER))", con2)
        finally:
            con2.close()

    cap = cached(config, "fr_unit_capacity_p999", db_key(config), _cap)
    reg = reg[reg["fuel_type"].isin(_FUEL2TECH)].copy()
    capm = dict(zip(cap["unit_id"], cap["cap"]))
    reg["tech"] = reg["fuel_type"].map(_FUEL2TECH)
    reg["capacity_mw"] = reg["eic_code"].map(capm)
    reg = reg[(reg["capacity_mw"].fillna(0) > 0)]
    if year is not None:
        live = active_units(config, year)
        if live:                                  # année non couverte ⇒ pas de filtre plutôt qu'un parc vide
            reg = reg[reg["eic_code"].isin(live)]
    return (reg.rename(columns={"eic_code": "unit_id", "canonical_name": "name"})
            [["unit_id", "name", "tech", "capacity_mw"]].reset_index(drop=True))
