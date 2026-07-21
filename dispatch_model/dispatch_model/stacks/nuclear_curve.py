"""Offre nucléaire FR en **courbe de tranches** — la dégénérescence des prix vient de là.

Le nucléaire était un bloc unique à 7,0 €/MWh (coût du combustible). Conséquence mesurée par
`lp.diagnostics` sur 2024 : le nucléaire est le bloc marginal **78,6 % des heures, à exactement
7,0 €/MWh**. Le prix français du modèle est donc quasi binaire, la distribution s'effondre sur une valeur,
et le markup de l'étape vii devient insoluble — il ne peut pas transformer un point en distribution.

La cause est arithmétique : 63 GW offerts à un prix unique contre ~9 GW d'hydraulique différenciée. Tant
que 63 GW partagent un seul prix, le dual du bilan y colle.

**Ce n'est pas une correction cosmétique : le parc nucléaire *a* une courbe d'offre, et on l'observe.**
Part de la capacité **disponible** (installé − indisponibilité REMIT) réellement produite, moyenne
pondérée 2019/2022/2023/2024 :

    prix     <-1     0-5   5-10  10-20  20-30  30-40  40-60  60-80  80-120  >120
    part    0,738  0,825  0,851  0,871  0,891  0,912  0,920  0,952   0,979  1,026

L'amplitude de modulation croît avec la pénétration renouvelable : 0,16 en 2019, 0,27 en 2024. Ce n'est
pas du bruit, c'est du suivi de charge.

Trois lectures économiques de cette courbe :

1. **Un socle inflexible d'environ 74 % du disponible produit même à prix négatif.** Minimum technique,
   contraintes de xénon, gestion de fin de campagne : l'arrêt-redémarrage coûte plus cher que de produire
   à perte quelques heures. Ce socle s'offre donc *sous zéro*, et non comme un `min_gen_frac` dur — un
   plancher dur plancherait le prix à zéro et supprimerait la queue négative française, exactement le
   défaut corrigé côté hydraulique.
2. **La bande modulable a un coût d'opportunité croissant.** Descendre en puissance consomme de la marge
   de manœuvre pour la suite (transitoires xénon, cycles comptés dans la gestion du combustible), donc
   plus on module profond, plus il faut être payé cher pour le dernier MWh. C'est ce qui donne à l'offre
   sa pente — pas un ajustement.
3. **Le dernier percentile n'apparaît qu'en tension.** Au-delà de 120 €/MWh la part observée dépasse 1,
   ce qui signale surtout que l'indisponibilité REMIT est surestimée (déclassements partiels déclarés
   comme arrêts). La courbe est écrêtée à 100 %, pas extrapolée.

**Ce que la mesure ne donne pas.** On observe que le socle produit sous zéro, jamais *jusqu'où* il
descendrait — le marché ne l'a pas testé. `MUSTRUN_BID` est donc une hypothèse bornée par l'observation
(voir sa docstring), pas un paramètre calibré.

**Résultat mesuré** (backtest complet, |erreur baseload| moyenne sur les six zones) :

    variante                                              2019    2024
    bloc unique a 7 EUR/MWh                              13,00   29,87
    courbe, prix = borne basse de classe                  6,47   26,35
    courbe, borne basse + cout combustible ajoute         5,98   25,15
    courbe, borne basse planchee au cout combustible      6,43   25,95
    courbe, prix = MOYENNE observee de la classe          5,93   25,68   <- retenu

FR 2019 : −19,6 % → −0,3 %. FR 2024 : −51,2 % → −34,9 %. Corrélation FR 2019 0,782 → 0,841, 2024
0,643 → 0,761. Part des heures FR dans une classe de prix de 1 €/MWh : 67,8 % → ~20 % en 2024.

Le prix retenu est la **moyenne observée de la classe**, pas sa borne basse : la capacité qui apparaît
dans [80, 120) a un prix de réserve *dans* l'intervalle, pas à 80 ; la borne basse biaise donc chaque
tranche vers le bas d'une demi-classe.

Deux choix de méthode qui vont contre le score, et pourquoi :

- La variante « + coût combustible » gagne 2024 de 0,5 point. Elle est écartée : en 2024 le modèle est
  massivement biaisé vers le bas pour des causes étrangères au nucléaire (CH +37 %, DE −32 %, ES −28 %
  inchangés dans toutes les variantes), donc **tout ce qui remonte les prix gagne mécaniquement**.
  2019, où le modèle est quasi non biaisé, est le discriminant propre — et la moyenne de classe y gagne.
  Elle est en outre la seule variante sans constante d'ajustement.
- **La queue négative française n'apparaît toujours pas** (0 heure modélisée contre 352 observées en
  2024), dans *aucune* variante. Le socle offre pourtant à −40 €/MWh. Le verrou n'est donc pas le prix
  d'offre nucléaire mais le mécanisme d'export identifié en S1c — prédiction faite ici et démentie par
  la mesure, à ne pas ré-attribuer au nucléaire.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np
import pandas as pd

from .revealed import SupplyCurve, curve_from_shares, empirical_shares
from .revealed import expand_stack as _expand

#: classes de prix : plus fines que pour l'hydraulique là où le nucléaire module réellement (0-40 €/MWh),
#: puisque c'est cette plage qui porte la formation des prix français hors tension.
PRICE_BINS = (-np.inf, 0.0, 5.0, 10.0, 20.0, 30.0, 40.0, 60.0, 80.0, 120.0, np.inf)

#: Prix d'offre du socle inflexible (€/MWh). **Hypothèse bornée par l'observation, pas une calibration.**
#: Mesuré sur FR 2023-2024 : le parc produit 27,0 GW en moyenne sous −5 €/MWh et encore 25,3 GW sous
#: −50 €/MWh — il ne répond donc plus au prix dans cette plage. Le 5ᵉ centile des prix négatifs observés
#: est −40,6 €/MWh : c'est la profondeur que le parc a démontrablement traversée sans reculer davantage.
#: On la retient comme prix d'offre du socle. Une valeur plus basse serait tout aussi compatible avec
#: l'observation ; elle ne changerait le résultat que dans les rares heures sous −40.
MUSTRUN_BID = -40.0

#: Prix de la tranche résiduelle : capacité disponible jamais observée en production. Alignée sur
#: l'hydraulique (`hydro.water_value.SCARCITY_WV`) faute d'observation, et volontairement au-dessus du
#: SRMC thermique. En pratique la courbe nucléaire sature avant, donc cette tranche est presque toujours
#: vide — elle n'existe que pour ne pas effacer de capacité réelle.
SCARCITY_BID = 200.0

#: Repli quand l'année n'a ni prix ni indisponibilité exploitables (projection, année sans REMIT).
#: Reproduit la forme moyenne mesurée 2019-2024, arrondie.
DEFAULT_CURVE = ((0.74, MUSTRUN_BID), (0.09, 0.0), (0.05, 10.0), (0.04, 30.0), (0.03, 60.0),
                 (0.05, 80.0))

#: Rampe horaire, en fraction de la capacité de la tranche — reportée depuis `fr_stack.FLEX` pour ne pas
#: perdre l'information. **Le LP multi-zone ne la lit pas** (seul `min_gen_frac` borne les colonnes de
#: production) : la contrainte de rampe nucléaire n'est donc pas active aujourd'hui, ni avant ni après ce
#: changement. La courbe d'offre joue un rôle voisin par un autre canal — elle rend la modulation profonde
#: coûteuse plutôt qu'impossible.
RAMP_FRAC = 0.05


def calibrate(price: pd.Series, output: pd.Series, available_mw) -> SupplyCurve:
    """Courbe d'offre nucléaire FR depuis (prix spot observé, production, capacité **disponible**).

    Le dénominateur est la capacité disponible, pas l'installée : en 2022 la moitié du parc était à
    l'arrêt, et rapporter la production à l'installé mélangerait indisponibilité fortuite et arbitrage
    économique — on lirait comme un refus de produire ce qui n'était qu'une indisponibilité.
    """
    shares = empirical_shares(price, output, available_mw, PRICE_BINS)
    return curve_from_shares("FR", shares, MUSTRUN_BID, SCARCITY_BID, DEFAULT_CURVE, tech="nuclear",
                             bid_from="mean")


def available_mw(config, year: int, installed_mw: float, index: pd.DatetimeIndex) -> pd.Series:
    """Capacité nucléaire disponible heure par heure = installé − indisponibilité REMIT (jour → heure).

    Sans donnée REMIT pour l'année, renvoie l'installé : la courbe sera alors calibrée sur un dénominateur
    trop large et donc trop plate, ce que `calibrate` ne peut pas corriger seul.
    """
    try:
        from pricemodeling.config import load_settings
        from pricemodeling.db import get_engine
        from pricemodeling.entsoe.unavailability import nuclear_unavailable_mw
        nu = nuclear_unavailable_mw(get_engine(load_settings().db_url), "FR", int(year))
    except Exception:                                    # noqa: BLE001 — pas de REMIT : on dégrade, on n'échoue pas
        nu = pd.Series(dtype=float)
    if nu.empty:
        return pd.Series(float(installed_mw), index=index)
    days = pd.DatetimeIndex(index).normalize().tz_localize(None).date
    un = pd.Series(days, index=index).map(nu).fillna(0.0).astype(float)
    return (float(installed_mw) - un).clip(lower=1.0)


def expand_stack(stack: pd.DataFrame, curve: SupplyCurve | None) -> pd.DataFrame:
    """Remplace les lignes `nuclear` du stack FR par les tranches de la courbe.

    **Le coût combustible n'est PAS ajouté** (`vom=0`), contrairement à l'hydraulique. Le prix de tranche
    est lu sur le marché : c'est le prix auquel la capacité a été *observée* produire, donc un prix
    d'offre tout compris, combustible dedans. Y rajouter `NUCLEAR_FUEL_EUR_MWH` double-compterait —
    mesuré : cela remontait la deuxième tranche de 0 à 7 €/MWh et interdisait donc structurellement les
    prix négatifs français, que la première tranche était pourtant censée rendre possibles.

    Les 56 lignes unitaires disparaissent : elles portaient toutes le même coût et la même
    disponibilité, donc elles n'apportaient aucune information au LP, seulement des colonnes.
    """
    return _expand(stack, curve, "FR", "nuclear", vom=0.0, ramp_frac=RAMP_FRAC, unit_prefix="mod")


@lru_cache(maxsize=8)
def _cache(key: tuple) -> dict:
    return {}


def load_curve(config, year: int, installed_mw: float) -> SupplyCurve | None:
    """Calibre (et mémorise) la courbe de l'année sur les données FR observées.

    Renvoie `None` si prix ou production manquent : sans observation on laisse le bloc unique en place
    plutôt que d'imposer une forme par défaut à un backtest — c'est un backtest qui doit rester mesurable.
    """
    cache = _cache((id(config), int(year), round(float(installed_mw), 3)))
    if "curve" in cache:
        return cache["curve"]
    from ..io.fr_history import load_fr_netload
    from ..rolling.backtest import _observed_prices

    cache["curve"] = None
    try:
        fr = load_fr_netload(config, f"{year}-01-01", f"{year + 1}-01-01").set_index("timestamp_utc")
        obs = _observed_prices(config, int(year), ["FR"]).get("FR")
    except (KeyError, ValueError):
        return None
    if obs is None or fr.empty or "gen_nuclear_mw" not in fr.columns:
        return None
    idx = fr.index.intersection(obs.dropna().index)
    if len(idx) < 24 * 30:
        return None
    cache["curve"] = calibrate(obs.reindex(idx), fr["gen_nuclear_mw"].reindex(idx),
                               available_mw(config, year, installed_mw, idx))
    return cache["curve"]
