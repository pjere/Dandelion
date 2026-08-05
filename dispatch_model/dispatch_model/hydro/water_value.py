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
import pandas as pd

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


def _arbitraged_wbar(curve) -> float:
    """Prix d'offre moyen pondéré des tranches ARBITRÉES (hors ancres débit réservé / rareté)."""
    if curve is None or len(curve.tranches) <= 2:
        return float("nan")
    t = np.asarray(curve.tranches, float)
    sh, bid = t[:, 0], t[:, 1]
    mid = np.argsort(bid)[1:-1]
    return float(np.average(bid[mid], weights=np.maximum(sh[mid], 1e-9)))


def seasonal_level_deltas(config, year: int, zones: tuple[str, ...]) -> dict[str, dict[str, float]]:
    """→ {zone: {'summer': Δ, 'rest': Δ}} — décalage de la valeur de l'eau par saison, en EUR/MWh.

    Le décalage est la différence entre le prix moyen des tranches arbitrées calibrées SUR LA SAISON et
    celui de la courbe annuelle : c'est exactement ce que la courbe annuelle unique efface. Appliqué par
    `shift_hydro_bids` via le `wv_delta` des constructeurs de fenêtres, il ne touche que les tranches
    arbitrées — le débit réservé et la rareté restent des ancres physiques.

    Mesuré 2024 (EUR/MWh) : ES +47,1 été / −18,0 reste (annuel 28,4 alors que l'eau d'été en vaut 75,5),
    IT_NORTH +29,6 / +7,8, CH −4,2 / +15,3, FR −5,4 / −6,5. La France est plate — la saisonnalité est
    ibérique et méditerranéenne, pas générique, ce qui est le contrôle qu'on veut sur ce genre de
    correction.

    NB : ceci ne double PAS le niveau SDP (#136). `synthesis.solve_levels` omet les zones sans SDP
    exploitable — mesuré, il ne rend que FR / CH / IT_NORTH, jamais ES — et ses écarts été-hiver valent
    −2,7 à −4,8 EUR/MWh, deux ordres de grandeur sous ce qui manque ici.
    """
    ann = load_curves(config, year, zones, season=None)
    out: dict[str, dict[str, float]] = {}
    for season in ("summer", "rest"):
        cur = load_curves(config, year, zones, season=season)
        for z in zones:
            a, s = _arbitraged_wbar(ann.get(z)), _arbitraged_wbar(cur.get(z))
            if np.isfinite(a) and np.isfinite(s):
                out.setdefault(z, {})[season] = round(s - a, 2)
    return out


@lru_cache(maxsize=8)
def _curve_cache(key: tuple) -> dict:
    return {}


#: Mois de la saison d'irrigation ibérique. La valeur de l'eau est SAISONNIÈRE et la courbe annuelle
#: unique l'ignorait complètement. Mesuré sur ES 2023-2025, production du parc de réservoir en % de la
#: capacité installée, par classe de prix :
#:
#:      saison        px<10   10-30   30-50   50-80    >80
#:      hiver DJFM     18.8    18.0    18.0    20.5    22.2     -> PLAT = débit imposé
#:      épaule         13.9    15.9    16.1    14.3    15.7
#:      été JJAS        3.6     4.8     6.8     6.9    11.3     -> x3 = arbitrage, eau rare
#:
#: L'hiver est domine par le debit imposé (production insensible au prix : crues, remplissage, lâchers
#: réglementaires) ; l'ÉTÉ est au contraire fortement arbitré — l'eau restante est rare et chère, et ce
#: qui part en irrigation part par le canal, pas par la turbine. La courbe annuelle unique impose une
#: tranche de débit réservé de ~15 % TOUTE L'ANNÉE : elle sous-estime l'hiver (~19 %) et surtout
#: SURESTIME L'ÉTÉ D'UN FACTEUR 4 (15 % contre 3,6 % mesurés), donc le modèle brade de l'hydraulique
#: espagnole tout l'été. C'est la piste principale du sur-tirage négatif ES (2233 heures projetées
#: contre 247 observées, moyenne 47 contre 63 EUR/MWh).
IRRIGATION_MONTHS = (6, 7, 8, 9)


def season_of(month: int) -> str:
    """→ 'summer' pendant la saison d'irrigation, 'rest' sinon. Clé de calibration saisonnière."""
    return "summer" if int(month) in IRRIGATION_MONTHS else "rest"


def seasonal_delta(config, year: int, zones: tuple[str, ...]) -> dict[str, dict[str, float]]:
    """→ {zone: {'summer': Δ€/MWh, 'rest': Δ€/MWh}} : décalage de la valeur de l'eau ARBITRÉE entre la
    calibration saisonnière et la calibration annuelle.

    Pourquoi un décalage de prix plutôt qu'une courbe saisonnière complète : les deux calibrations ne
    produisent pas le même NOMBRE de tranches (ES 2024 : 5 hors été, 9 en été), donc échanger la courbe
    par fenêtre changerait le nombre de lignes du stack — or les specs de flexibilité indexent ces
    lignes. `shift_hydro_bids` (déjà branché sur `wv_delta` dans `fr_window`/`nb_window`) déplace les
    prix d'offre sans toucher à la structure, ce qui porte l'essentiel de l'effet : en été l'eau
    ibérique est rare et chère, et la courbe annuelle la brade.

    Le décalage est la moyenne pondérée capacité des tranches ARBITRÉES seules — les ancres physiques
    (débit réservé, rareté) sont exclues, comme dans `shift_hydro_bids`.
    """
    ann = load_curves(config, year, zones, season=None)
    out: dict[str, dict[str, float]] = {}
    for season in ("summer", "rest"):
        cur = load_curves(config, year, zones, season=season)
        for z in zones:
            a, s = ann.get(z), cur.get(z)
            if a is None or s is None:
                continue
            base, seas = _arbitrated_mean(a), _arbitrated_mean(s)
            if base is None or seas is None:
                continue
            out.setdefault(z, {})[season] = round(seas - base, 3)
    return out


def _arbitrated_mean(curve: SupplyCurve) -> float | None:
    """Moyenne pondérée capacité des tranches hors ancres (débit réservé le moins cher, rareté)."""
    tr = list(curve.tranches)
    if len(tr) <= 2:
        return None
    mid = sorted(tr, key=lambda t: t[1])[1:-1]
    w = sum(sh for sh, _ in mid)
    return sum(sh * b for sh, b in mid) / w if w > 0 else None


def load_curves(config, year: int, zones: tuple[str, ...],
                season: str | None = None) -> dict[str, SupplyCurve]:
    """Calibre (et mémorise) la courbe de chaque zone sur les prix et productions observés de `year`.

    `season` ∈ {None, 'summer', 'rest'} : None calibre sur l'année entière (comportement historique) ;
    sinon la calibration ne retient que les heures de la saison, ce qui donne à l'été sa vraie tranche
    de débit réservé au lieu de la moyenne annuelle. Voir `IRRIGATION_MONTHS`.

    Imports différés : ce module est appelé depuis la construction des fenêtres du LP, et les lecteurs
    ENTSO-E/backtest importent eux-mêmes les stacks — un import direct fermerait le cycle.
    """
    cache = _curve_cache((id(config), int(year), zones, season))
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
        if season is not None:                     # restreindre à la saison avant de calibrer
            keep = [t for t in idx if season_of(t.month) == season]
            # sous MIN_HOURS_PER_BIN par classe la courbe devient du bruit : garder l'annuelle
            idx = pd.DatetimeIndex(keep) if len(keep) >= 24 * 30 else idx
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
