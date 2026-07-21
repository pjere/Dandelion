"""Courbe d'offre **révélée** : un parc n'offre pas en bloc unique, il offre en tranches.

Moteur générique, extrait de `hydro.water_value` pour servir aussi le nucléaire. Le principe est le même
dans les deux cas et ne dépend pas de la technologie :

    on observe, par classe de prix, la part de la capacité *disponible* que le parc a effectivement
    produite ; cette part, rendue monotone, **est** la courbe d'offre du parc.

Ce que ça capture qu'un coût variable unique ne peut pas :

1. **Le socle inflexible s'offre sous zéro.** Une part du parc produit quelle que soit la rémunération
   (débit réservé pour l'hydraulique, minimum technique et gestion de campagne pour le nucléaire). Elle
   doit s'offrir *en dessous de zéro* — un `min_gen_frac` dur plancherait le prix à zéro et supprimerait
   la queue négative (mesuré sur DE_LU : 847 → 16 heures négatives).
2. **L'élasticité est graduelle.** Le parc n'est pas une machine unique mais un ensemble d'installations
   aux coûts d'opportunité différents. D'où une *courbe*, et non un scalaire — c'est elle qui donne au LP
   un dual qui varie au lieu de coller à une valeur unique.

Le prix d'offre de chaque tranche est la **borne basse** de la classe de prix où cette capacité est
apparue : c'est le prix à partir duquel on l'a observée produire.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

#: colonne qui écrase le SRMC des tranches — un coût d'opportunité, pas un coût variable
BID_COL = "opportunity_bid_eur_mwh"
MIN_HOURS_PER_BIN = 20


@dataclass(frozen=True)
class SupplyCurve:
    """Courbe d'offre d'un parc : tranches (part de la capacité, prix d'offre €/MWh).

    `capacity_mismatch` est vrai quand la production observée dépassait la capacité de référence : la
    courbe a été écrêtée à 100 %, et c'est la capacité en amont qui est à corriger, pas la courbe.
    """
    zone: str
    tranches: tuple[tuple[float, float], ...]
    capacity_mismatch: bool = False
    tech: str = ""

    @property
    def total_share(self) -> float:
        return float(sum(s for s, _ in self.tranches))


def empirical_shares(price: pd.Series, output: pd.Series, capacity, bins,
                     min_hours: int = MIN_HOURS_PER_BIN) -> pd.DataFrame:
    """Part moyenne de la capacité produite par classe de prix. Classes trop peu peuplées écartées.

    `capacity` est un scalaire (parc dont la capacité disponible ne bouge pas dans l'année) **ou** une
    série horaire — c'est le cas du nucléaire, où rapporter la production à l'installé mélangerait
    indisponibilité fortuite et arbitrage économique.
    """
    cap = capacity if isinstance(capacity, pd.Series) else pd.Series(float(capacity), index=output.index)
    cap = pd.to_numeric(cap, errors="coerce")
    if not (cap > 0).any():
        return pd.DataFrame(columns=["lo", "share", "hours"])
    df = pd.DataFrame({"p": price, "q": pd.to_numeric(output, errors="coerce") / cap.where(cap > 0)}).dropna()
    if df.empty:
        return pd.DataFrame(columns=["lo", "share", "hours"])
    df["bin"] = pd.cut(df["p"], bins=list(bins), right=False)
    g = df.groupby("bin", observed=True).agg(share=("q", "mean"), hours=("q", "size"),
                                             p_mean=("p", "mean"))
    g = g[g["hours"] >= min_hours]
    return pd.DataFrame({"lo": [iv.left for iv in g.index], "share": g["share"].to_numpy(),
                         "hours": g["hours"].to_numpy(), "p_mean": g["p_mean"].to_numpy()})


def curve_from_shares(zone: str, shares: pd.DataFrame, floor_bid: float, scarcity_bid: float,
                      default: tuple, tech: str = "", bid_from: str = "edge") -> SupplyCurve:
    """Parts par classe de prix → tranches monotones cumulables.

    La monotonie est imposée par cummax : une courbe d'offre ne peut pas décroître quand le prix monte, et
    le bruit d'échantillonnage en produit (CH : 0,297 à 40-60 puis 0,225 à 60-80). Sans cette contrainte on
    obtiendrait des tranches à part négative, économiquement absurdes.

    `bid_from` choisit le prix représentatif de chaque classe :

    - `"edge"` — la borne basse. Conservateur, et **biaisé vers le bas d'environ une demi-classe** : la
      capacité qui apparaît dans [80, 120) a un prix de réserve quelque part dans l'intervalle, pas à 80.
    - `"mean"` — le prix moyen effectivement observé dans la classe. C'est l'estimateur du prix de
      réserve révélé, sans constante d'ajustement. Mesuré sur le nucléaire FR : corriger ce biais vaut
      1 à 4 points d'erreur baseload, là où une constante ajoutée uniformément corrigeait trop en bas de
      courbe et pas assez en haut.

    `floor_bid` est le prix de la première tranche — le socle inflexible, dont l'observation ne donne que
    la borne (on le voit produire sous zéro, pas *jusqu'où* il descendrait). `scarcity_bid` price le
    reliquat de capacité jamais observé en production : l'omettre reviendrait à effacer de la capacité
    réelle (mesuré sur CH : tronquer à 39,9 % retirait 3,5 GW et faisait passer l'erreur baseload de
    +29,7 à +68,5 %).
    """
    if shares.empty:
        return SupplyCurve(zone, default, tech=tech)
    s = shares.sort_values("lo").copy()
    cum = np.maximum.accumulate(s["share"].to_numpy(float))
    # Une part cumulée > 1 signifie que la production observée dépasse la capacité de référence : c'est une
    # incohérence de données, pas une courbe. On écrête plutôt que de fabriquer de la capacité.
    s["share"] = np.clip(cum, 0.0, 1.0)
    col = "p_mean" if bid_from == "mean" and "p_mean" in s.columns else "lo"
    lo = s[col].to_numpy(float)
    lo = np.where(np.isfinite(lo), lo, floor_bid)
    lo = np.maximum.accumulate(lo)             # le prix représentatif suit l'ordre des classes
    lo[0] = floor_bid                          # la première tranche est le socle inflexible
    inc = np.diff(np.concatenate([[0.0], s["share"].to_numpy(float)]))
    tr = [(float(a), float(b)) for a, b in zip(inc, lo) if a > 1e-4]
    reste = 1.0 - sum(a for a, _ in tr)
    if reste > 1e-4:                           # conserver la capacité totale (cf. scarcity_bid)
        tr.append((reste, float(scarcity_bid)))
    return SupplyCurve(zone, tuple(tr) or default, capacity_mismatch=float(cum[-1]) > 1.0 + 1e-9, tech=tech)


def tranche_rows(zone: str, capacity: float, curve: SupplyCurve, tech: str, vom: float = 0.0,
                 ramp_frac: float | None = None, unit_prefix: str = "wv") -> pd.DataFrame:
    """Lignes de stack remplaçant le bloc unique par la courbe.

    Le VOM s'ajoute au prix d'offre : le coût d'opportunité s'empile sur le coût variable réel.
    `min_gen_frac` reste à 0 — c'est le **prix d'offre** de la première tranche, pas une contrainte dure,
    qui reproduit le socle inflexible. La différence est essentielle : une tranche qui offre en dessous de
    zéro laisse le prix y descendre, là où un plancher dur l'y bloquerait.

    `ramp_frac` n'est émis que s'il est fourni : le LP multi-zone ne le lit pas (seul `min_gen_frac` borne
    les colonnes de production), donc l'ajouter par défaut ne ferait qu'élargir le DataFrame.
    """
    rows = []
    for i, (share, bid) in enumerate(curve.tranches):
        cap = float(share) * float(capacity)
        if cap <= 0:
            continue
        r = {"unit_id": f"{zone}_{tech}_{unit_prefix}{i}", "zone": zone, "tech": tech,
             "capacity_mw": cap, "efficiency": np.nan, "min_gen_frac": 0.0,
             BID_COL: float(bid) + float(vom)}
        if ramp_frac is not None:
            r["ramp_frac"] = float(ramp_frac)
        rows.append(r)
    return pd.DataFrame(rows)


def expand_stack(stack: pd.DataFrame, curve: SupplyCurve | None, zone: str, tech: str,
                 vom: float = 0.0, ramp_frac: float | None = None,
                 unit_prefix: str = "wv") -> pd.DataFrame:
    """Remplace les lignes `tech` de `stack` par les tranches de la courbe.

    Sans courbe, le stack est renvoyé inchangé — on ne substitue jamais un défaut arbitraire à une donnée
    absente.
    """
    if curve is None or not (stack["tech"] == tech).any():
        return stack
    cap = float(stack.loc[stack["tech"] == tech, "capacity_mw"].sum())
    keep = stack[stack["tech"] != tech].copy()
    new = tranche_rows(zone, cap, curve, tech, vom=vom, ramp_frac=ramp_frac, unit_prefix=unit_prefix)
    if new.empty:
        return stack
    cols = list(dict.fromkeys(list(keep.columns) + list(new.columns)))
    return pd.concat([keep.reindex(columns=cols), new.reindex(columns=cols)], ignore_index=True)


def apply_bids(stack: pd.DataFrame, srmc_col: str = "srmc_eur_mwh") -> pd.DataFrame:
    """Écrase le SRMC des tranches par leur prix d'offre, **après** calcul des SRMC thermiques."""
    if BID_COL not in stack.columns:
        return stack
    out = stack.copy()
    m = out[BID_COL].notna()
    out.loc[m, srmc_col] = out.loc[m, BID_COL].to_numpy(float)
    return out
