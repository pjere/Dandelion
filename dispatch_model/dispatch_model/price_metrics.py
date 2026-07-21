"""Métriques de valorisation : ce qu'on regarde vraiment quand on se sert de ces prix.

Le backtest historique score l'erreur baseload, des quantiles et des comptages. C'est nécessaire mais
insuffisant : **personne ne valorise un actif au prix baseload**. Un PPA solaire se règle au *prix de
capture* — la moyenne pondérée par la production — et un modèle peut avoir un baseload juste tout en
manquant complètement la corrélation prix↔production, donc la cannibalisation.

C'est précisément le risque ici. Le dispatch reproduit le milieu de la distribution mais rate les deux
queues : sur 2024 il produit 0 heure négative en FR contre 352 observées, 0 en BE contre 403, et
quasiment aucun pic (0-1 contre 25-129). Un modèle sans heures négatives **surestime structurellement la
capture** des filières fatales, et l'erreur croît avec la pénétration RES — donc avec l'horizon de
projection. Ces métriques rendent ce biais visible au lieu de le laisser se cacher derrière une moyenne
annuelle correcte.

La distance à la courbe de charge classée résume la compression de distribution d'un seul nombre : deux
séries de même moyenne mais d'étalement différent y divergent, là où l'erreur baseload les déclare
identiques.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def capture_price(price: pd.Series, production: pd.Series) -> float:
    """Prix de capture : moyenne des prix pondérée par la production. Le prix réellement perçu."""
    df = pd.DataFrame({"p": price, "q": production}).dropna()
    df = df[df["q"] > 0]
    if df.empty or df["q"].sum() <= 0:
        return float("nan")
    return float((df["p"] * df["q"]).sum() / df["q"].sum())


def capture_rate(price: pd.Series, production: pd.Series) -> float:
    """Capture rapportée au baseload. < 1 = cannibalisation (le solaire produit quand les prix sont bas)."""
    base = float(pd.Series(price).dropna().mean())
    if not np.isfinite(base) or abs(base) < 1e-9:
        return float("nan")
    return capture_price(price, production) / base


def duration_curve(price: pd.Series, n: int = 100) -> np.ndarray:
    """Courbe de charge classée ré-échantillonnée sur `n` points — comparable entre séries de tailles différentes."""
    s = pd.Series(price).dropna().sort_values(ascending=False).to_numpy(float)
    if s.size == 0:
        return np.full(n, np.nan)
    return np.interp(np.linspace(0, 1, n), np.linspace(0, 1, s.size), s)


def duration_curve_distance(model: pd.Series, observed: pd.Series, n: int = 100) -> dict:
    """Écart entre courbes classées : erreur moyenne, biais, et où il se concentre.

    `tail_low` / `tail_high` isolent les deux déciles extrêmes — c'est là que se joue la compression de
    distribution, et une erreur moyenne agrégée la dissimule.
    """
    m, o = duration_curve(model, n), duration_curve(observed, n)
    d = m - o
    k = max(1, n // 10)
    return {"mae": float(np.nanmean(np.abs(d))), "bias": float(np.nanmean(d)),
            "tail_high": float(np.nanmean(d[:k])),      # heures les plus chères
            "tail_low": float(np.nanmean(d[-k:]))}      # heures les moins chères


def tail_counts(price: pd.Series, spike_threshold: float = 200.0) -> dict:
    """Comptages de queue : négatives, nulles, pics. Les heures que le LP continu ne sait pas produire."""
    s = pd.Series(price).dropna()
    return {"n": int(s.size), "negative": int((s < 0).sum()), "zero": int((s == 0).sum()),
            "spike": int((s > spike_threshold).sum())}


def valuation_report(model: pd.Series, observed: pd.Series,
                     production: dict[str, pd.Series] | None = None,
                     spike_threshold: float = 200.0) -> pd.DataFrame:
    """Rapport comparant modèle et observé sur les métriques qui portent une décision.

    `production` donne les profils horaires par filière (solaire, éolien…) ; les prix de capture sont
    évalués sur **les mêmes profils** pour les deux séries de prix, de sorte que l'écart mesure bien
    l'erreur de prix et non une différence de production.
    """
    rows = []
    base_m, base_o = float(model.dropna().mean()), float(observed.dropna().mean())
    rows.append({"metric": "baseload", "model": base_m, "observed": base_o, "delta": base_m - base_o})
    for name, q in (production or {}).items():
        cm, co = capture_price(model, q), capture_price(observed, q)
        rows.append({"metric": f"capture_{name}", "model": cm, "observed": co, "delta": cm - co})
        rm, ro = capture_rate(model, q), capture_rate(observed, q)
        rows.append({"metric": f"capture_rate_{name}", "model": rm, "observed": ro, "delta": rm - ro})
    tm, to = tail_counts(model, spike_threshold), tail_counts(observed, spike_threshold)
    for k in ("negative", "spike"):
        rows.append({"metric": f"hours_{k}", "model": tm[k], "observed": to[k],
                     "delta": tm[k] - to[k]})
    dc = duration_curve_distance(model, observed)
    for k, v in dc.items():
        rows.append({"metric": f"duration_{k}", "model": np.nan, "observed": np.nan, "delta": v})
    return pd.DataFrame(rows)
