"""Valeur de l'eau **structurelle** : arbitrage turbiner maintenant / stocker pour plus tard.

Ce module remplace l'approche descriptive de `water_value.py` (préférence révélée : on observait le
comportement passé et on en déduisait un prix de réserve implicite). Celle-ci ne modélisait aucun
arbitrage et souffrait d'une circularité subie — on calibrait sur des prix observés une courbe censée
produire les prix du modèle.

Ici la valeur de l'eau est ce qu'elle est réellement : **la dérivée de la fonction valeur de Bellman**.

    V_t(S) = E[ max_u  R_t(u)  +  V_{t+1}( min(S + I_t − u, S_max) ) ]
    λ_t(S) = ∂V_t/∂S

où `S` est le stock (MWh), `u` le volume turbiné sur la semaine, `I_t` l'apport aléatoire. `λ_t(S)` est
le prix auquel il faut offrir l'eau : en deçà, mieux vaut stocker.

Trois éléments font le travail que l'ajustement de courbe ne faisait pas :

1. **`R_t(u)` est concave.** Sur une semaine, l'exploitant place son eau sur les heures les plus chères
   d'abord, donc le revenu marginal décroît avec le volume turbiné. On le construit depuis la monotone de
   prix de la semaine. C'est cette concavité qui produit l'étalement de la production — structurellement,
   pas par calibrage.
2. **La récursion arrière** donne une valeur qui dépend *du stock et de la saison* : élevée en fin d'hiver
   à stock bas, faible à la fonte. La courbe empirique était annuelle et statique.
3. **Le déversement est explicite** : l'eau qui dépasse `S_max` est perdue (`min(..., S_max)`), ce qui
   fait naturellement chuter la valeur de l'eau quand le réservoir est plein — le producteur turbine alors
   même à prix bas plutôt que de déverser.

La condition terminale est **cyclique** (V_53 = V_1), résolue par itérations sur l'année jusqu'à
convergence : un réservoir saisonnier n'a pas de fin d'horizon naturelle.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

HOURS_PER_WEEK = 168
N_STORAGE = 60          # points de discrétisation du stock
N_RELEASE = 60          # points de discrétisation du turbinage
MAX_SWEEPS = 40         # itérations sur l'année pour le point fixe cyclique
TOL_EUR = 0.05          # convergence : variation max de la valeur marginale, €/MWh


@dataclass(frozen=True)
class Reservoir:
    """Réservoir équivalent d'une zone (agrégation standard : agréger puis désagréger)."""
    zone: str
    s_min: float                    # MWh — stock mort / réserve réglementaire
    s_max: float                    # MWh — capacité utile
    p_max_mw: float                 # MW — puissance de turbinage installée
    # Débit réservé : turbinage qui a lieu quelle que soit la rémunération (obligations de débit,
    # irrigation, navigation). C'est une contrainte **physique**, pas un arbitrage — sans elle la SDP
    # ne décrit qu'une optimisation économique pure et sous-estime l'utilisation du parc de 15 à 25 %
    # (mesuré sur FR, CH, ES, IT : utilisation impliquée 0,196/0,207/0,132/0,321 contre 0,228/0,275/
    # 0,167/0,398 observées, biais de même signe partout).
    min_release_mw: float = 0.0

    @property
    def u_max(self) -> float:
        return float(self.p_max_mw) * HOURS_PER_WEEK

    @property
    def u_min(self) -> float:
        return float(self.min_release_mw) * HOURS_PER_WEEK


def revenue_curve(week_prices: np.ndarray, p_max_mw: float, u_grid: np.ndarray) -> np.ndarray:
    """Revenu R(u) de turbiner `u` MWh sur la semaine, alloué aux heures les plus chères.

    Concave par construction : les premiers MWh vont aux heures chères, les suivants à des heures de moins
    en moins bien payées. C'est cette concavité qui étale la production — un bloc à coût unique la
    concentrerait sur la pointe.
    """
    p = np.sort(np.asarray(week_prices, float))[::-1]        # monotone décroissante
    cap = float(p_max_mw)                                     # MWh turbinables par heure
    energy = np.arange(1, p.size + 1) * cap                   # énergie cumulée si on remplit les h meilleures
    revenue = np.cumsum(p * cap)
    # interpolation linéaire entre les paliers horaires ; au-delà du total turbinable, plateau
    return np.interp(u_grid, np.concatenate([[0.0], energy]), np.concatenate([[0.0], revenue]))


def _inflow_scenarios(inflows: pd.DataFrame, week: int, n_max: int = 12) -> np.ndarray:
    """Apports historiques observés pour cette semaine, toutes années — scénarios empiriques."""
    v = inflows.loc[inflows["week"] == week, "inflow_mwh"].to_numpy(float)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return np.array([0.0])
    return v if v.size <= n_max else np.quantile(v, np.linspace(0.05, 0.95, n_max))


def solve(res: Reservoir, weekly_prices: dict[int, np.ndarray], inflows: pd.DataFrame,
          n_storage: int = N_STORAGE, n_release: int = N_RELEASE,
          max_sweeps: int = MAX_SWEEPS, tol: float = TOL_EUR) -> dict:
    """Récursion arrière cyclique. Renvoie la grille de stock, V_t(S) et λ_t(S) = ∂V/∂S.

    `weekly_prices[w]` est la série des prix horaires de la semaine `w` (1-52) ; `inflows` a les colonnes
    [week, inflow_mwh].
    """
    S = np.linspace(res.s_min, res.s_max, n_storage)
    weeks = sorted(weekly_prices)
    nW = len(weeks)
    R = {w: revenue_curve(weekly_prices[w], res.p_max_mw, np.linspace(0.0, res.u_max, n_release))
         for w in weeks}
    U = np.linspace(0.0, res.u_max, n_release)
    scen = {w: _inflow_scenarios(inflows, w) for w in weeks}

    V = np.zeros((nW, n_storage))
    sweeps_done, delta = 0, np.inf
    for _ in range(max_sweeps):
        sweeps_done += 1
        V_prev = V.copy()
        # Balayage arrière **en place** (Gauss-Seidel) : la semaine i consomme la valeur de la semaine i+1
        # déjà remise à jour dans ce même balayage, donc l'information remonte toute l'année d'un coup.
        # Une mise à jour Jacobi (lire l'itération précédente) ne la propage que d'une semaine : mesuré,
        # lambda etait encore en mouvement apres 40 balayages au lieu de converger en quelques-uns.
        for i in range(nW - 1, -1, -1):
            w = weeks[i]
            Vnext = V[(i + 1) % nW]                       # cyclique : après la semaine 52 vient la 1
            acc = np.zeros(n_storage)
            for inflow in scen[w]:
                avail = S + float(inflow)                  # (n_storage,)
                # u faisable : au moins le débit réservé, au plus la puissance installée, sans
                # descendre sous s_min. Le plancher cède devant la disponibilité en eau — on ne turbine
                # pas une eau qu'on n'a pas.
                u_hi = np.minimum(res.u_max, np.maximum(avail - res.s_min, 0.0))
                u_lo = np.minimum(res.u_min, u_hi)
                u = np.clip(U[None, :], u_lo[:, None], u_hi[:, None])   # (n_storage, n_release)
                # déversement explicite : l'eau au-dessus de s_max est perdue
                s_next = np.minimum(avail[:, None] - u, res.s_max)
                val = np.interp(u, U, R[w]) + np.interp(s_next, S, Vnext)
                acc += val.max(axis=1)
            V[i] = acc / len(scen[w])
        # Itération sur la valeur RELATIVE. Sans actualisation, V croît d'un gain annuel constant à
        # chaque balayage et ne converge jamais en niveau ; ses différences, elles, convergent. On retire
        # donc une référence à chaque tour. La valeur de l'eau étant un gradient, ce décalage est sans
        # effet sur elle — et il rend le critère d'arrêt bien posé.
        V -= V[0, 0]
        delta = np.abs(np.gradient(V, S, axis=1) - np.gradient(V_prev, S, axis=1)).max()
        if delta < tol:
            break
    lam = np.gradient(V, S, axis=1)                        # €/MWh : valeur marginale du stock
    return {"storage_mwh": S, "weeks": weeks, "value": V, "water_value": lam,
            "sweeps": sweeps_done, "converged": bool(delta < tol)}


def water_value_at(sol: dict, week: int, storage_mwh: float) -> float:
    """λ_t(S) interpolée — le prix auquel offrir l'eau cette semaine, à ce niveau de stock."""
    weeks = sol["weeks"]
    i = weeks.index(week) if week in weeks else int(np.argmin(np.abs(np.array(weeks) - week)))
    return float(np.interp(storage_mwh, sol["storage_mwh"], sol["water_value"][i]))


def infer_inflows(stock: pd.Series, generation_mwh: pd.Series, s_max: float,
                  smooth_weeks: int = 1) -> pd.DataFrame:
    """Apports par bilan hydraulique : `apport = ΔStock + production`, lissé puis borné.

    Deux biais corrigés ici, tous deux mesurés :

    *Déversement.* Quand le réservoir est plein, l'eau excédentaire part sans laisser de trace dans le
    stock : le bilan sous-estime les crues. On borne le stock à `s_max` (hypothèse assumée) et on écarte
    les semaines à moins de 2 % du plafond.

    *Base de production.* `generation_mwh` doit couvrir **toute** la production tirant sur la retenue,
    turbinage des STEP compris. N'y mettre que la filière « reservoir » sous-estime les soutirages, donc
    les apports, donc rend l'eau trop chère — c'est la cause du biais mesuré (utilisation impliquée 15 à
    25 % sous l'observée dans les quatre zones).

    `smooth_weeks` ne lisse que le bruit de décalage entre relevé et fenêtre de production ; il ne corrige
    aucun biais. Attention au sens : l'écrêtage à zéro **gonfle** les apports (il remonte les bilans
    négatifs), lisser avant écrêtage les réduit donc. Laissé à 1 par défaut pour cette raison.
    """
    s = pd.Series(stock).astype(float).sort_index()
    g = pd.Series(generation_mwh).astype(float).reindex(s.index).fillna(0.0)
    ds = s.diff().shift(-1)                                  # S_{t+1} − S_t
    inflow = (ds + g).dropna()
    if smooth_weeks and smooth_weeks > 1:
        inflow = inflow.rolling(smooth_weeks, center=True, min_periods=1).mean()
    full = s.reindex(inflow.index) > 0.98 * float(s_max)
    out = pd.DataFrame({"inflow_mwh": inflow.clip(lower=0.0),
                        "week": pd.DatetimeIndex(inflow.index).isocalendar().week.astype(int).to_numpy(),
                        "suspect_spill": full.to_numpy()})
    return out[~out["suspect_spill"]].drop(columns="suspect_spill")
