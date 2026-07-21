"""Métriques de valorisation : elles doivent rendre visible ce que le baseload cache.

Le test central est `test_capture_distingue_ce_que_le_baseload_confond` : deux modèles de même moyenne
annuelle mais de corrélation prix↔production opposée doivent être départagés, sinon la métrique ne sert
à rien.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from dispatch_model.price_metrics import (
    capture_price,
    capture_rate,
    duration_curve,
    duration_curve_distance,
    tail_counts,
    valuation_report,
)


def _h(vals):
    return pd.Series(vals, index=pd.date_range("2024-01-01", periods=len(vals), freq="h", tz="UTC"))


def test_capture_pondere_par_la_production():
    price, prod = _h([10.0, 100.0]), _h([3.0, 1.0])
    assert capture_price(price, prod) == (10 * 3 + 100 * 1) / 4


def test_capture_ignore_les_heures_sans_production():
    price, prod = _h([10.0, 1000.0]), _h([5.0, 0.0])
    assert capture_price(price, prod) == 10.0      # l'heure chère ne compte pas si on ne produit pas


def test_cannibalisation_donne_un_taux_inferieur_a_un():
    """Le solaire produit quand les prix sont bas : capture < baseload."""
    price, prod = _h([0.0, 20.0, 80.0, 100.0]), _h([10.0, 8.0, 1.0, 0.5])
    assert capture_rate(price, prod) < 1.0


def test_capture_distingue_ce_que_le_baseload_confond():
    """Deux modèles de même moyenne, corrélations opposées : le baseload les déclare identiques."""
    prod = _h([10.0, 10.0, 1.0, 1.0])
    bon = _h([0.0, 0.0, 100.0, 100.0])        # prix bas quand on produit (réaliste)
    mauvais = _h([100.0, 100.0, 0.0, 0.0])    # l'inverse
    assert bon.mean() == mauvais.mean()                      # indiscernables au baseload
    assert capture_price(bon, prod) < capture_price(mauvais, prod)   # mais pas à la capture


def test_courbe_classee_est_decroissante_et_de_taille_fixe():
    dc = duration_curve(_h([5.0, 100.0, -20.0, 40.0]), n=50)
    assert dc.size == 50
    assert np.all(np.diff(dc) <= 1e-9)


def test_distance_nulle_pour_series_identiques():
    s = _h(list(np.linspace(-10, 200, 48)))
    d = duration_curve_distance(s, s)
    assert abs(d["mae"]) < 1e-9 and abs(d["bias"]) < 1e-9


def test_distance_detecte_la_compression_de_distribution():
    """Même moyenne, étalement écrasé : c'est exactement le défaut du LP continu."""
    obs = _h([-50.0, 0.0, 50.0, 100.0, 300.0])
    comprime = _h([80.0, 80.0, 80.0, 80.0, 80.0])
    assert abs(comprime.mean() - obs.mean()) < 1e-9          # biais moyen nul
    d = duration_curve_distance(comprime, obs)
    assert d["mae"] > 50                                      # mais les queues divergent
    assert d["tail_high"] < 0                                 # pics sous-estimés
    assert d["tail_low"] > 0                                  # négatifs manqués


def test_comptages_de_queue():
    t = tail_counts(_h([-5.0, 0.0, 50.0, 250.0]), spike_threshold=200.0)
    assert (t["negative"], t["zero"], t["spike"], t["n"]) == (1, 1, 1, 4)


def test_rapport_couvre_capture_queues_et_courbe():
    obs = _h([-10.0, 20.0, 60.0, 250.0])
    mdl = _h([5.0, 25.0, 55.0, 120.0])
    rep = valuation_report(mdl, obs, production={"solar": _h([0.0, 5.0, 3.0, 0.0])})
    noms = set(rep["metric"])
    assert {"baseload", "capture_solar", "capture_rate_solar", "hours_negative", "hours_spike"} <= noms
    assert rep.loc[rep.metric == "hours_negative", "delta"].iloc[0] == -1   # le modele en rate une
