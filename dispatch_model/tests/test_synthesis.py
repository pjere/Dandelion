"""Synthèse SDP × empirique : le recentrage garde la forme, met le niveau, préserve les ancres.

Tests sans base de données — sur la logique de recentrage (`shift_hydro_bids`, `_empirical_level`), pas
sur la résolution SDP (couverte par `test_bellman`) ni le backtest complet (couvert par le golden).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from dispatch_model.hydro.synthesis import _empirical_level, shift_hydro_bids, solve_levels
from dispatch_model.hydro.water_value import SCARCITY_WV, HydroCurve

_VOM = 1.0                                   # cf. synthesis._VOM


def _hydro_stack(bids):
    """Stack minimal : des tranches hydrauliques + un bloc gaz témoin qui ne doit jamais bouger."""
    rows = [{"unit_id": f"CH_h{i}", "tech": "hydro_reservoir", "srmc_eur_mwh": b} for i, b in enumerate(bids)]
    rows.append({"unit_id": "CH_g", "tech": "gas", "srmc_eur_mwh": 90.0})
    return pd.DataFrame(rows)


def test_niveau_empirique_est_la_moyenne_des_tranches_arbitrees():
    """Hors débit réservé (1re) et hors rareté : moyenne pondérée des seules tranches arbitrées."""
    c = HydroCurve("CH", ((0.15, -15.0), (0.10, 25.0), (0.10, 60.0), (0.15, SCARCITY_WV)))
    assert np.isclose(_empirical_level(c), (0.10 * 25 + 0.10 * 60) / 0.20)   # = 42.5


def test_niveau_none_si_pas_de_tranche_arbitree():
    c = HydroCurve("CH", ((1.0, -15.0),))                # que du débit réservé
    assert _empirical_level(c) is None


def test_decalage_positif_monte_les_tranches_arbitrees():
    st = _hydro_stack([-14.0, 26.0, 61.0, 121.0, SCARCITY_WV + _VOM])
    out = shift_hydro_bids(st, 30.0)
    b = out.loc[out.tech == "hydro_reservoir", "srmc_eur_mwh"].to_numpy()
    assert b[0] == -14.0                                 # débit réservé : ancre basse, inchangé
    assert np.isclose(b[-1], SCARCITY_WV + _VOM)         # rareté : ancre haute, inchangée
    assert np.allclose(b[1:4], [56.0, 91.0, 151.0])      # arbitrées décalées de +30
    assert (out.loc[out.tech == "gas", "srmc_eur_mwh"] == 90.0).all()   # le gaz ne bouge pas


def test_monotonie_preservee_par_un_decalage_negatif():
    st = _hydro_stack([-14.0, 26.0, 61.0, 121.0, SCARCITY_WV + _VOM])
    b = shift_hydro_bids(st, -80.0).loc[lambda d: d.tech == "hydro_reservoir", "srmc_eur_mwh"].to_numpy()
    assert np.all(np.diff(b) >= -1e-9)                   # jamais décroissant
    assert b[0] == -14.0 and b[1] > b[0]                 # reste strictement au-dessus du débit réservé


def test_decalage_extreme_borne_sous_la_rarete():
    """Un delta énorme ne doit pas pousser les tranches arbitrées au-dessus de la rareté."""
    st = _hydro_stack([-14.0, 26.0, 61.0, SCARCITY_WV + _VOM])
    b = shift_hydro_bids(st, 500.0).loc[lambda d: d.tech == "hydro_reservoir", "srmc_eur_mwh"].to_numpy()
    assert np.all(b <= SCARCITY_WV + _VOM + 1e-9)
    assert np.all(np.diff(b) >= -1e-9)


def test_decalage_nul_ou_non_fini_laisse_le_stack_intact():
    st = _hydro_stack([-14.0, 26.0, 61.0])
    assert shift_hydro_bids(st, 0.0).equals(st)
    assert shift_hydro_bids(st, float("nan")).equals(st)


def test_stack_sans_hydro_inchange():
    st = pd.DataFrame([{"unit_id": "g", "tech": "gas", "srmc_eur_mwh": 90.0}])
    assert shift_hydro_bids(st, 40.0).equals(st)


def test_une_seule_tranche_hydro_non_decalee():
    """Avec une seule tranche (le débit réservé), rien à recentrer : elle est l'ancre."""
    st = _hydro_stack([-14.0])
    assert shift_hydro_bids(st, 40.0).equals(st)


def test_solve_levels_sans_courbe_rend_dict_vide():
    """Aucune courbe → aucune SDP à résoudre, pas d'accès base. Renvoie {} proprement."""
    assert solve_levels(None, 2024, {}, ("FR", "CH")) == {}
