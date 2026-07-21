"""Valeur de l'eau : la courbe doit reproduire ce que le bloc unique ne sait pas faire.

Le test qui compte est `test_premiere_tranche_offre_sous_zero` : c'est ce qui distingue cette approche
d'un plancher dur. Un `min_gen_frac` forcerait le prix à zéro et supprimerait la queue négative (mesuré :
DE_LU tombait de 847 à 16 heures négatives) ; une tranche qui *offre* à prix négatif laisse le prix
descendre.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from dispatch_model.hydro.water_value import (
    DEFAULT_CURVE,
    MUSTFLOW_BID,
    SCARCITY_WV,
    HydroCurve,
    apply_water_value,
    calibrate,
    curve_from_shares,
    empirical_shares,
    expand_stack,
    tranche_rows,
)


def _obs(pairs):
    idx = pd.date_range("2024-01-01", periods=len(pairs), freq="h", tz="UTC")
    return (pd.Series([p for p, _ in pairs], index=idx),
            pd.Series([q for _, q in pairs], index=idx))


def test_parts_empiriques_par_classe_de_prix():
    p, q = _obs([(-5, 100.0)] * 30 + [(50, 300.0)] * 30)
    s = empirical_shares(p, q, capacity=1000.0)
    assert len(s) == 2
    assert np.isclose(s["share"].iloc[0], 0.10)
    assert np.isclose(s["share"].iloc[1], 0.30)


def test_classes_trop_peu_peuplees_ecartees():
    p, q = _obs([(-5, 100.0)] * 3 + [(50, 300.0)] * 40)
    s = empirical_shares(p, q, capacity=1000.0)
    assert len(s) == 1                      # la classe à 3 heures ne calibre rien


def test_monotonie_imposee():
    """Le bruit produit des décroissances (CH : 0,297 puis 0,225) ; une courbe d'offre ne décroît pas."""
    shares = pd.DataFrame({"lo": [-np.inf, 25.0, 60.0], "share": [0.10, 0.30, 0.22], "hours": [50] * 3})
    c = curve_from_shares("CH", shares)
    assert all(s > 0 for s, _ in c.tranches)          # aucune tranche de part négative
    # la part *calibrée* est plafonnée au maximum atteint (0,30), pas rabattue sur 0,22 ; le solde part
    # dans la tranche résiduelle de rareté, qui préserve la capacité totale
    calibre = sum(s for s, w in c.tranches if w != SCARCITY_WV)
    assert np.isclose(calibre, 0.30)


def test_valeurs_de_l_eau_croissantes():
    shares = pd.DataFrame({"lo": [-np.inf, 25.0, 60.0], "share": [0.13, 0.24, 0.40], "hours": [50] * 3})
    c = curve_from_shares("CH", shares)
    wv = [w for _, w in c.tranches]
    assert wv == sorted(wv)


def test_premiere_tranche_offre_sous_zero():
    """Le débit réservé s'offre sous zéro — c'est ce qui préserve la queue négative."""
    shares = pd.DataFrame({"lo": [-np.inf, 25.0], "share": [0.13, 0.30], "hours": [50, 50]})
    c = curve_from_shares("CH", shares)
    assert c.tranches[0][1] == MUSTFLOW_BID
    assert c.tranches[0][1] < 0


def test_courbe_par_defaut_si_pas_de_donnees():
    c = calibrate("XX", pd.Series(dtype=float), pd.Series(dtype=float), capacity=1000.0)
    assert c.tranches == DEFAULT_CURVE


def test_capacite_totale_conservee_par_les_tranches():
    c = HydroCurve("CH", ((0.15, -15.0), (0.10, 25.0), (0.05, 60.0)))
    rows = tranche_rows("CH", 1000.0, c)
    assert np.isclose(rows["capacity_mw"].sum(), 300.0)      # 30 % de 1000
    assert (rows["min_gen_frac"] == 0.0).all()               # aucune contrainte dure


def test_vom_s_ajoute_a_la_valeur_de_l_eau():
    rows = tranche_rows("CH", 1000.0, HydroCurve("CH", ((0.2, 40.0),)), vom=2.5)
    assert np.isclose(rows["water_value_eur_mwh"].iloc[0], 42.5)


def test_expansion_du_stack_remplace_le_bloc_unique():
    st = pd.DataFrame({"unit_id": ["CH_g", "CH_h"], "zone": "CH", "tech": ["gas", "hydro_reservoir"],
                       "capacity_mw": [500.0, 1000.0], "min_gen_frac": [0.15, 0.0]})
    out = expand_stack(st, {"CH": HydroCurve("CH", ((0.15, -15.0), (0.10, 25.0)))}, "CH")
    assert (out["tech"] == "hydro_reservoir").sum() == 2       # deux tranches
    assert np.isclose(out.loc[out.tech == "hydro_reservoir", "capacity_mw"].sum(), 250.0)
    assert (out["tech"] == "gas").sum() == 1                   # le reste intact


def test_zone_sans_courbe_reste_inchangee():
    """Sans calibration, on ne substitue pas un défaut arbitraire à une donnée absente."""
    st = pd.DataFrame({"unit_id": ["X"], "zone": "XX", "tech": ["hydro_reservoir"],
                       "capacity_mw": [1000.0], "min_gen_frac": [0.0]})
    assert expand_stack(st, {}, "XX").equals(st)


def test_application_ecrase_le_srmc_hydraulique_seulement():
    st = pd.DataFrame({"tech": ["gas", "hydro_reservoir"], "srmc_eur_mwh": [90.0, 1.0],
                       "water_value_eur_mwh": [np.nan, 41.0]})
    out = apply_water_value(st)
    assert out["srmc_eur_mwh"].tolist() == [90.0, 41.0]


def test_part_cumulee_ecretee_a_cent_pourcent():
    """Production observée > capacité déclarée = incohérence de données (FR 2024 : 155 %).
    On écrête plutôt que de fabriquer de la capacité, et on le signale."""
    shares = pd.DataFrame({"lo": [-np.inf, 25.0, 60.0], "share": [0.6, 1.2, 1.6], "hours": [50] * 3})
    c = curve_from_shares("FR", shares)
    assert c.total_share <= 1.0 + 1e-9
    assert c.capacity_mismatch is True


def test_pas_de_signalement_quand_la_courbe_est_saine():
    shares = pd.DataFrame({"lo": [-np.inf, 25.0], "share": [0.13, 0.30], "hours": [50, 50]})
    assert curve_from_shares("CH", shares).capacity_mismatch is False


def test_capacite_totale_preservee_par_la_tranche_residuelle():
    """La courbe empirique mesure la production habituelle, pas la capacité disponible. Tronquer efface
    de la capacité réelle : mesuré sur CH, 39,9 % offerts retiraient 3,5 GW et portaient l'erreur baseload
    de +29,7 à +68,5 %."""
    shares = pd.DataFrame({"lo": [-np.inf, 25.0], "share": [0.13, 0.30], "hours": [50, 50]})
    c = curve_from_shares("CH", shares)
    assert np.isclose(c.total_share, 1.0)
    assert c.tranches[-1][1] == SCARCITY_WV


def test_tranche_residuelle_est_la_plus_chere():
    shares = pd.DataFrame({"lo": [-np.inf, 25.0, 120.0], "share": [0.2, 0.3, 0.45], "hours": [50] * 3})
    wv = [w for _, w in curve_from_shares("CH", shares).tranches]
    assert wv == sorted(wv) and wv[-1] == SCARCITY_WV
