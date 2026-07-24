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

Le moteur de calibration lui-même est dans `stacks.revealed` — il est générique et sert aussi au nucléaire
(`stacks.nuclear_curve`). Ici ne restent que les constantes propres à l'eau et le chargement par zone.
Voir aussi `hydro.bellman` : la valeur de l'eau **structurelle** par programmation dynamique, qui donne le
niveau de λ là où cette courbe-ci en donne la dispersion.
"""
from __future__ import annotations

from dataclasses import replace
from functools import lru_cache

import numpy as np

from ..stacks import revealed
from ..stacks.revealed import BID_COL, MIN_HOURS_PER_BIN, SupplyCurve, apply_bids

#: bornes de prix pour la calibration (EUR/MWh) — resserrées là où la densité d'heures est forte
PRICE_BINS = (-np.inf, 0.0, 10.0, 25.0, 40.0, 60.0, 80.0, 120.0, np.inf)
#: valeur de l'eau attribuée à la première tranche : le débit réservé, qui s'écoule même payé négativement
MUSTFLOW_BID = -15.0
#: valeur de l'eau de la tranche residuelle. La courbe empirique mesure la production *habituelle*, pas la
#: capacite *disponible* : le reste du parc peut produire en tension, on ne l'observe simplement jamais aux
#: prix historiques. Cette valeur n'est pas calibree (l'observation ne la contient pas) : c'est une
#: hypothese, volontairement au-dessus du SRMC thermique.
SCARCITY_WV = 200.0
DEFAULT_CURVE = ((0.15, MUSTFLOW_BID), (0.10, 25.0), (0.10, 60.0), (0.10, 120.0))

#: proxy de valeur de l'eau pour les clusters voisins sans prix ni stock (split de DE_REST). Un cluster
#: virtuel n'a ni prix observé (courbe empirique impossible) ni série `entsoe_hydro_storage` (SDP Bellman
#: impossible) : ses deux voies de valorisation sont fermées et son hydro de lac serait offerte au plancher
#: ~1 EUR/MWh. Elle déferlerait alors dans la zone modélisée la plus chère qu'elle borde — l'Italie, via
#: IT↔AT/SI — écrasant son prix (mesuré : IT_NORTH baseload −11,7 → −15,6 % avec le split). On emprunte donc
#: la courbe révélée d'une zone modélisée de même hydrologie. Ciblé : seul AT_SI porte une hydro de lac
#: significative (~1,3 GW alpine) ; NL/DK ~0, PL_CZ modeste et non alpin, donc laissés au défaut.
_WATER_VALUE_PROXY = {"AT_SI": "CH"}

#: nom historique de la courbe hydraulique ; le moteur est générique depuis l'ajout du nucléaire
HydroCurve = SupplyCurve
#: `apply_water_value` reste le nom d'appel côté hydraulique
apply_water_value = apply_bids

__all__ = ["BID_COL", "DEFAULT_CURVE", "MIN_HOURS_PER_BIN", "MUSTFLOW_BID", "PRICE_BINS", "SCARCITY_WV",
           "HydroCurve", "apply_water_value", "calibrate", "curve_from_shares", "empirical_shares",
           "expand_stack", "load_curves", "tranche_rows"]


def empirical_shares(price, output, capacity, bins=PRICE_BINS, min_hours: int = MIN_HOURS_PER_BIN):
    """Part moyenne de capacité produite par classe de prix, aux bornes hydrauliques."""
    return revealed.empirical_shares(price, output, capacity, bins, min_hours)


def curve_from_shares(zone: str, shares) -> SupplyCurve:
    """Parts par classe de prix → tranches, avec le débit réservé et la rareté propres à l'eau."""
    return revealed.curve_from_shares(zone, shares, MUSTFLOW_BID, SCARCITY_WV, DEFAULT_CURVE,
                                      tech="hydro_reservoir")


def calibrate(zone: str, price, output, capacity: float) -> SupplyCurve:
    """Calibre la courbe d'une zone sur ses observations."""
    return curve_from_shares(zone, empirical_shares(price, output, capacity))


def tranche_rows(zone: str, capacity: float, curve: SupplyCurve, tech: str = "hydro_reservoir",
                 vom: float = 1.0):
    """Lignes de stack remplaçant le bloc hydraulique unique par la courbe."""
    return revealed.tranche_rows(zone, capacity, curve, tech, vom=vom)


def expand_stack(stack, curves: dict[str, SupplyCurve], zone: str, tech: str = "hydro_reservoir"):
    """Remplace les lignes `tech` de `stack` par les tranches de valeur de l'eau de la zone."""
    return revealed.expand_stack(stack, curves.get(zone), zone, tech, vom=1.0)


@lru_cache(maxsize=8)
def _curve_cache(key: tuple) -> dict:
    return {}


def load_curves(config, year: int, zones: tuple[str, ...]) -> dict[str, SupplyCurve]:
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
    # clusters sans prix ni stock : emprunter la courbe d'une zone modélisée de même hydrologie (cf.
    # _WATER_VALUE_PROXY) plutôt que laisser leur hydro de lac offerte au plancher ~1 EUR/MWh.
    for cluster, proxy in _WATER_VALUE_PROXY.items():
        if cluster in zones and cluster not in cache and cache.get(proxy) is not None:
            cache[cluster] = replace(cache[proxy], zone=cluster)
    return cache
