"""Contrôle de divergence RTE/ENTSO-E : il doit voir les défauts réels et se taire sur le reste.

Un contrôle qui crie au loup finit ignoré, donc les tests portent autant sur ce qui NE doit PAS être
signalé que sur ce qui doit l'être. Sans base : on éprouve la logique de comparaison directement.
"""
from __future__ import annotations

import pandas as pd

from pricemodeling.qc import (
    _KNOWN_MONTHS,
    FLOOR_MW,
    KNOWN_INCIDENTS,
    MIN_HOURS,
    REL_TOL,
    unexplained,
)


def _rows(recs):
    """Reproduit les colonnes calculées par source_divergence sur des lignes fabriquées."""
    d = pd.DataFrame(recs)
    ref = d[["rte", "entsoe"]].abs().max(axis=1)
    d["diff"] = d["rte"] - d["entsoe"]
    d["rel"] = d["diff"] / ref.where(ref > 0)
    d["material"] = (ref >= FLOOR_MW) & (d["rel"].abs() > REL_TOL) & (d["n"] >= MIN_HOURS)
    d["known"] = d["month"].isin(_KNOWN_MONTHS)
    return d


def test_gros_creux_rte_est_signale():
    d = _rows([{"month": "2025-03", "column": "prod_nuclear", "rte": 26000, "entsoe": 40000, "n": 720}])
    assert len(unexplained(d)) == 1


def test_rte_anormalement_haut_est_signale_aussi():
    """Le repli ne corrige que les creux ; le contrôle, lui, doit être symétrique."""
    d = _rows([{"month": "2025-04", "column": "prod_fossil_gas", "rte": 9000, "entsoe": 3000, "n": 720}])
    u = unexplained(d)
    assert len(u) == 1
    assert u["diff"].iloc[0] > 0


def test_sources_concordantes_ne_declenchent_rien():
    d = _rows([{"month": "2025-05", "column": "prod_nuclear", "rte": 38210, "entsoe": 38200, "n": 744}])
    assert unexplained(d).empty


def test_ecart_sous_le_seuil_relatif_ignore():
    d = _rows([{"month": "2025-06", "column": "prod_solar", "rte": 850, "entsoe": 1000, "n": 720}])
    assert unexplained(d).empty          # 15 % < 20 %


def test_petite_filiere_protegee_par_le_plancher():
    d = _rows([{"month": "2025-07", "column": "prod_fossil_oil", "rte": 10, "entsoe": FLOOR_MW - 1,
                "n": 720}])
    assert unexplained(d).empty


def test_episode_trop_court_ignore():
    """Quelques heures ne font pas un incident de publication."""
    d = _rows([{"month": "2025-08", "column": "prod_nuclear", "rte": 1000, "entsoe": 40000,
                "n": MIN_HOURS - 1}])
    assert unexplained(d).empty


def test_incident_connu_est_masque():
    """Sept/oct 2024 est instruit, corrigé et signalé : il ne doit plus polluer le rapport."""
    d = _rows([{"month": "2024-09", "column": "prod_nuclear", "rte": 26107, "entsoe": 37896, "n": 720}])
    assert d["material"].all() and d["known"].all()
    assert unexplained(d).empty


def test_un_incident_connu_ne_masque_que_ses_mois():
    d = _rows([
        {"month": "2024-09", "column": "prod_nuclear", "rte": 26107, "entsoe": 37896, "n": 720},
        {"month": "2024-11", "column": "prod_nuclear", "rte": 26107, "entsoe": 37896, "n": 720},
    ])
    u = unexplained(d)
    assert list(u["month"]) == ["2024-11"]


def test_les_incidents_connus_sont_documentes():
    """Une entrée sans justification écrite rouvrirait la porte à masquer un vrai défaut."""
    assert KNOWN_INCIDENTS
    for inc in KNOWN_INCIDENTS:
        assert inc.months and len(inc.reason) > 40


def test_pompage_absent_du_perimetre():
    """RTE net / ENTSO-E brut : divergence permanente et légitime, hors périmètre."""
    from pricemodeling.merge.build_master import ENTSOE_FALLBACK

    assert "prod_hydro_pumped_storage" not in ENTSOE_FALLBACK
