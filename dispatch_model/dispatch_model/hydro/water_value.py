"""Valeur de l'eau : l'hydraulique de lac offerte en **courbe de tranches**, pas en bloc unique.

Pourquoi ce module existe. L'hydraulique de lac était modélisée comme un bloc unique au VOM (~1 EUR/MWh)
sous budget énergétique dur. Dans un LP, un budget dur et une valeur de l'eau scalaire sont **équivalents**
(le dual du budget *est* la valeur de l'eau), et le résultat est un comportement tout-ou-rien : la
totalité du budget hebdomadaire part sur les heures de pointe, à pleine puissance. La réalité étale.

Mesuré sur 2024 (part de capacité produite selon le prix) :

    prix        <0    0-10   10-25   25-40   40-60   60-80  80-120   >120
    CH        0,134   0,151  0,159   0,239   0,297   0,225   0,299   0,399
    ES        0,134   0,170  0,179   0,163   0,156   0,172   0,163   0,197
    IT_NORTH    -     0,247  0,274   0,254   0,226   0,228   0,392   0,490

Deux faits que le bloc unique ne peut pas reproduire :

1. **Même à prix négatif, 13 à 25 % de la capacité produit.** C'est le débit réservé — de l'eau qui doit
   s'écouler quelle que soit la rémunération. Elle s'offre donc *en dessous de zéro*, ce qui la rend
   compatible avec la formation de prix négatifs, contrairement à un `min_gen_frac` dur qui, lui, plancherait
   le prix à zéro et supprimerait la queue négative (mesuré : DE_LU passait de 847 à 16 heures négatives).
2. **L'élasticité est modeste et graduelle** : un parc n'est pas un réservoir unique mais un ensemble de
   retenues aux coûts d'opportunité différents. D'où une *courbe*, et non un scalaire.

La courbe est calibrée par zone sur les couples (prix observé, production observée), rendue monotone, puis
convertie en tranches. Le budget énergétique reste en garde-fou mais ne devrait plus mordre.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
import pandas as pd

#: bornes de prix pour la calibration (EUR/MWh) — resserrées là où la densité d'heures est forte
PRICE_BINS = (-np.inf, 0.0, 10.0, 25.0, 40.0, 60.0, 80.0, 120.0, np.inf)
#: valeur de l'eau attribuée à chaque tranche : borne basse de la classe, la première offrant sous zéro
#: (débit réservé : l'eau s'écoule même payée négativement)
MUSTFLOW_BID = -15.0
MIN_HOURS_PER_BIN = 20
#: valeur de l'eau de la tranche residuelle. La courbe empirique mesure la production *habituelle*, pas la
#: capacite *disponible* : le reste du parc peut produire en tension, on ne l'observe simplement jamais aux
#: prix historiques. L'omettre revient a effacer de la capacite reelle — mesure sur CH, ou tronquer a 39,9 %
#: retirait 3,5 GW et faisait passer l'erreur baseload de +29,7 a +68,5 %. Cette valeur n'est pas calibree
#: (l'observation ne la contient pas) : c'est une hypothese, volontairement au-dessus du SRMC thermique.
SCARCITY_WV = 200.0
DEFAULT_CURVE = ((0.15, MUSTFLOW_BID), (0.10, 25.0), (0.10, 60.0), (0.10, 120.0))


@dataclass(frozen=True)
class HydroCurve:
    """Courbe d'offre hydraulique d'une zone : tranches (part de capacité, valeur de l'eau EUR/MWh).

    `capacity_mismatch` est vrai quand la production observée dépassait la capacité déclarée : la courbe a
    été écrêtée à 100 % et la capacité du stack de cette zone est à corriger en amont.
    """
    zone: str
    tranches: tuple[tuple[float, float], ...]
    capacity_mismatch: bool = False

    @property
    def total_share(self) -> float:
        return float(sum(s for s, _ in self.tranches))


def empirical_shares(price: pd.Series, output: pd.Series, capacity: float,
                     bins=PRICE_BINS, min_hours: int = MIN_HOURS_PER_BIN) -> pd.DataFrame:
    """Part moyenne de capacité produite par classe de prix. Les classes trop peu peuplées sont écartées."""
    if capacity <= 0:
        return pd.DataFrame(columns=["lo", "share", "hours"])
    df = pd.DataFrame({"p": price, "q": pd.to_numeric(output, errors="coerce") / capacity}).dropna()
    if df.empty:
        return pd.DataFrame(columns=["lo", "share", "hours"])
    df["bin"] = pd.cut(df["p"], bins=list(bins), right=False)
    g = df.groupby("bin", observed=True)["q"].agg(["mean", "count"])
    g = g[g["count"] >= min_hours]
    return pd.DataFrame({"lo": [iv.left for iv in g.index], "share": g["mean"].to_numpy(),
                         "hours": g["count"].to_numpy()})


def curve_from_shares(zone: str, shares: pd.DataFrame) -> HydroCurve:
    """Parts par classe de prix → tranches monotones cumulables.

    La monotonie est imposée par cummax : une courbe d'offre ne peut pas décroître quand le prix monte, et
    le bruit d'échantillonnage en produit (CH : 0,297 à 40-60 puis 0,225 à 60-80). Sans cette contrainte on
    obtiendrait des tranches à part négative, économiquement absurdes.
    """
    if shares.empty:
        return HydroCurve(zone, DEFAULT_CURVE)
    s = shares.sort_values("lo").copy()
    cum = np.maximum.accumulate(s["share"].to_numpy(float))
    # Une part cumulée > 1 signifie que la production observée dépasse la capacité déclarée au stack :
    # c'est une incohérence de données, pas une courbe. Mesuré sur FR 2024 : 155 % (production de lac
    # jusqu'à 5 212 MW contre 2 140 MW au stack, alors que le parc lac+éclusée réel avoise 8-10 GW).
    # On écrête plutôt que de fabriquer de la capacité, et `capacity_mismatch` le signale à l'appelant.
    s["share"] = np.clip(cum, 0.0, 1.0)
    lo = s["lo"].to_numpy(float)
    lo = np.where(np.isfinite(lo), lo, MUSTFLOW_BID)
    lo[0] = MUSTFLOW_BID                       # la première tranche est le débit réservé
    inc = np.diff(np.concatenate([[0.0], s["share"].to_numpy(float)]))
    tr = [(float(a), float(b)) for a, b in zip(inc, lo) if a > 1e-4]
    reste = 1.0 - sum(a for a, _ in tr)
    if reste > 1e-4:                       # conserver la capacite totale (cf. SCARCITY_WV)
        tr.append((reste, SCARCITY_WV))
    return HydroCurve(zone, tuple(tr) or DEFAULT_CURVE,
                      capacity_mismatch=float(cum[-1]) > 1.0 + 1e-9)


def calibrate(zone: str, price: pd.Series, output: pd.Series, capacity: float) -> HydroCurve:
    """Calibre la courbe d'une zone sur ses observations."""
    return curve_from_shares(zone, empirical_shares(price, output, capacity))


def tranche_rows(zone: str, capacity: float, curve: HydroCurve, tech: str = "hydro_reservoir",
                 vom: float = 1.0) -> pd.DataFrame:
    """Lignes de stack remplaçant le bloc hydraulique unique par la courbe.

    Le VOM s'ajoute à la valeur de l'eau : le coût d'opportunité s'empile sur le coût variable réel.
    `min_gen_frac` reste à 0 — c'est le **prix d'offre** de la première tranche, pas une contrainte dure,
    qui reproduit le débit réservé. La différence est essentielle : une tranche qui offre à -15 EUR/MWh
    laisse le prix descendre sous zéro, là où un plancher dur l'y bloquerait.
    """
    rows = []
    for i, (share, wv) in enumerate(curve.tranches):
        cap = float(share) * float(capacity)
        if cap <= 0:
            continue
        rows.append({"unit_id": f"{zone}_{tech}_wv{i}", "zone": zone, "tech": tech,
                     "capacity_mw": cap, "efficiency": np.nan, "min_gen_frac": 0.0,
                     "water_value_eur_mwh": float(wv) + float(vom)})
    return pd.DataFrame(rows)


def expand_stack(stack: pd.DataFrame, curves: dict[str, HydroCurve], zone: str,
                 tech: str = "hydro_reservoir") -> pd.DataFrame:
    """Remplace les lignes `tech` de `stack` par les tranches de valeur de l'eau de la zone.

    Sans courbe pour la zone, le stack est renvoyé inchangé — on ne substitue jamais un défaut arbitraire
    à une donnée absente.
    """
    curve = curves.get(zone)
    if curve is None or not (stack["tech"] == tech).any():
        return stack
    cap = float(stack.loc[stack["tech"] == tech, "capacity_mw"].sum())
    keep = stack[stack["tech"] != tech].copy()
    new = tranche_rows(zone, cap, curve, tech)
    if new.empty:
        return stack
    cols = list(dict.fromkeys(list(keep.columns) + list(new.columns)))
    return pd.concat([keep.reindex(columns=cols), new.reindex(columns=cols)], ignore_index=True)


def apply_water_value(stack: pd.DataFrame, srmc_col: str = "srmc_eur_mwh") -> pd.DataFrame:
    """Écrase le SRMC des tranches hydrauliques par leur valeur de l'eau, après calcul des SRMC thermiques."""
    if "water_value_eur_mwh" not in stack.columns:
        return stack
    out = stack.copy()
    m = out["water_value_eur_mwh"].notna()
    out.loc[m, srmc_col] = out.loc[m, "water_value_eur_mwh"].to_numpy(float)
    return out


@lru_cache(maxsize=8)
def _curve_cache(key: tuple) -> dict:
    return {}


def load_curves(config, year: int, zones: tuple[str, ...]) -> dict[str, HydroCurve]:
    """Calibre (et mémorise) la courbe de chaque zone sur les prix et productions observés de `year`.

    Imports différés : ce module est appelé depuis la construction des fenêtres du LP, et les lecteurs
    ENTSO-E/backtest importent eux-mêmes les stacks — un import direct fermerait le cycle.
    """
    cache = _curve_cache((id(config), int(year), zones))
    if cache:
        return cache
    from ..io.entsoe_hist import load_generation_hist
    from ..neighbours.blocks import build_neighbour_stack, constituents
    from ..rolling.backtest import _observed_prices
    from ..rolling.windows import fr_stack_base

    obs = _observed_prices(config, year, list(zones))
    for z in zones:
        o = obs.get(z)
        if o is None:
            continue
        try:
            st = fr_stack_base(config, year) if z == "FR" else build_neighbour_stack(config, z, year)
        except (KeyError, ValueError):
            continue
        cap = float(st.loc[st["tech"] == "hydro_reservoir", "capacity_mw"].sum())
        if cap <= 0:
            continue
        g = load_generation_hist(config, year, zones=constituents(z))
        if g.empty:
            continue
        p = g.pivot_table(index="timestamp_utc", columns="tech", values="gen_mw", aggfunc="sum")
        if "hydro_reservoir" not in p.columns:
            continue
        idx = p.index.intersection(o.index)
        c = calibrate(z, o.reindex(idx), p["hydro_reservoir"].reindex(idx), cap)
        if c.capacity_mismatch:
            # la capacité déclarée est incohérente avec la production observée : la courbe a été écrêtée,
            # mais le stack de cette zone est à corriger en amont plutôt qu'ici
            pass
        cache[z] = c
    return cache
