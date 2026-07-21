"""Courbe d'offre nucléaire : les propriétés qui cassent la dégénérescence des prix.

Le test central est `test_le_bloc_unique_devient_une_courbe_a_prix_multiples` : avant, 63 GW portaient un
prix unique et le dual du bilan y collait 78,6 % des heures ; c'est cette unicité qu'il faut supprimer.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from dispatch_model.stacks.costs import NUCLEAR_FUEL_EUR_MWH
from dispatch_model.stacks.nuclear_curve import (
    DEFAULT_CURVE,
    MUSTRUN_BID,
    PRICE_BINS,
    SCARCITY_BID,
    calibrate,
    expand_stack,
)
from dispatch_model.stacks.revealed import BID_COL, SupplyCurve, empirical_shares


def _serie(pairs):
    idx = pd.date_range("2024-01-01", periods=len(pairs), freq="h", tz="UTC")
    return (pd.Series([p for p, _ in pairs], index=idx),
            pd.Series([q for _, q in pairs], index=idx))


def test_socle_inflexible_offre_sous_zero():
    """~74 % du disponible produit même à prix négatif : ce socle doit s'offrir *sous* zéro, sinon le
    prix français ne peut plus devenir négatif (c'est la leçon du plancher dur, cf. hydraulique)."""
    p, q = _serie([(-20.0, 740.0)] * 40 + [(50.0, 900.0)] * 40)
    c = calibrate(p, q, 1000.0)
    assert c.tranches[0][1] == MUSTRUN_BID < 0        # la calibration donne le coût d'opportunité seul
    assert np.isclose(c.tranches[0][0], 0.74, atol=0.01)


def test_le_bloc_unique_devient_une_courbe_a_prix_multiples():
    """Le cœur du correctif : plusieurs prix d'offre, donc un dual qui peut varier."""
    st = pd.DataFrame({"unit_id": [f"N{i}" for i in range(3)] + ["G"], "zone": "FR",
                       "tech": ["nuclear"] * 3 + ["gas"], "capacity_mw": [20000.0] * 3 + [5000.0],
                       "min_gen_frac": [0.25] * 3 + [0.0]})
    c = SupplyCurve("FR", ((0.7, -40.0), (0.1, 0.0), (0.1, 30.0), (0.1, 80.0)), tech="nuclear")
    out = expand_stack(st, c)
    nuc = out[out.tech == "nuclear"]
    assert len(nuc) == 4
    assert nuc[BID_COL].nunique() == 4                       # plus de prix unique
    assert np.isclose(nuc["capacity_mw"].sum(), 60000.0)     # capacité conservée
    assert (out["tech"] == "gas").sum() == 1


def test_le_plancher_dur_disparait():
    """Le socle passe d'une contrainte (`min_gen_frac`=0,25) à un prix d'offre. Un plancher dur
    plancherait le prix à zéro et supprimerait la queue négative."""
    st = pd.DataFrame({"unit_id": ["N"], "zone": "FR", "tech": ["nuclear"],
                       "capacity_mw": [60000.0], "min_gen_frac": [0.25]})
    out = expand_stack(st, SupplyCurve("FR", ((0.7, -40.0), (0.3, 20.0)), tech="nuclear"))
    assert (out["min_gen_frac"] == 0.0).all()


def test_le_combustible_n_est_pas_ajoute_au_prix_calibre():
    """Le prix de tranche est lu sur le marché : il inclut déjà le combustible. Le rajouter remontait la
    deuxième tranche de 0 à 7 €/MWh et interdisait donc les prix négatifs français."""
    st = pd.DataFrame({"unit_id": ["N"], "zone": "FR", "tech": ["nuclear"],
                       "capacity_mw": [1000.0], "min_gen_frac": [0.25]})
    out = expand_stack(st, SupplyCurve("FR", ((0.7, MUSTRUN_BID), (0.3, 0.0)), tech="nuclear"))
    assert np.isclose(out[BID_COL].iloc[0], MUSTRUN_BID)
    assert np.isclose(out[BID_COL].iloc[1], 0.0)
    assert out[BID_COL].max() < NUCLEAR_FUEL_EUR_MWH


def test_prix_d_offre_croissants():
    """Une courbe d'offre ne décroît pas : la monotonie est imposée, pas espérée."""
    p, q = _serie([(-5.0, 700.0)] * 30 + [(20.0, 900.0)] * 30 + [(60.0, 850.0)] * 30)
    bids = [b for _, b in calibrate(p, q, 1000.0).tranches]
    assert bids == sorted(bids)


def test_capacite_disponible_variable_est_acceptee():
    """En 2022 la moitié du parc était à l'arrêt : rapporter la production à l'installé lirait comme un
    refus de produire ce qui n'était qu'une indisponibilité."""
    idx = pd.date_range("2024-01-01", periods=80, freq="h", tz="UTC")
    p = pd.Series([10.0] * 40 + [60.0] * 40, index=idx)
    q = pd.Series([450.0] * 40 + [900.0] * 40, index=idx)
    dispo = pd.Series([500.0] * 40 + [1000.0] * 40, index=idx)
    s = empirical_shares(p, q, dispo, PRICE_BINS)
    # 450/500 = 900/1000 = 0,9 : l'utilisation est identique, seule la disponibilité change
    assert np.allclose(s["share"].to_numpy(), 0.9)


def test_capacite_installee_seule_ecrase_le_signal():
    """Contrôle du test précédent : avec un dénominateur fixe, la même série produit une fausse pente."""
    idx = pd.date_range("2024-01-01", periods=80, freq="h", tz="UTC")
    p = pd.Series([10.0] * 40 + [60.0] * 40, index=idx)
    q = pd.Series([450.0] * 40 + [900.0] * 40, index=idx)
    s = empirical_shares(p, q, 1000.0, PRICE_BINS)
    assert s["share"].iloc[0] < s["share"].iloc[-1] - 0.3


def test_part_superieure_a_un_ecretee_et_signalee():
    """La production dépasse parfois le disponible REMIT (déclassements partiels déclarés comme arrêts).
    On écrête plutôt que de fabriquer de la capacité."""
    p, q = _serie([(10.0, 800.0)] * 30 + [(150.0, 1040.0)] * 30)
    c = calibrate(p, q, 1000.0)
    assert c.total_share <= 1.0 + 1e-9
    assert c.capacity_mismatch is True


def test_courbe_par_defaut_si_pas_d_observation():
    c = calibrate(pd.Series(dtype=float), pd.Series(dtype=float), 1000.0)
    assert c.tranches == DEFAULT_CURVE
    assert c.tranches[0][1] == MUSTRUN_BID


def test_capacite_residuelle_conservee():
    """Ne jamais effacer de capacité réelle : le reliquat jamais observé part en tranche de rareté."""
    p, q = _serie([(10.0, 700.0)] * 30 + [(60.0, 800.0)] * 30)
    c = calibrate(p, q, 1000.0)
    assert np.isclose(c.total_share, 1.0)
    assert c.tranches[-1][1] == SCARCITY_BID


def test_stack_sans_nucleaire_inchange():
    st = pd.DataFrame({"unit_id": ["G"], "zone": "FR", "tech": ["gas"],
                       "capacity_mw": [500.0], "min_gen_frac": [0.0]})
    assert expand_stack(st, SupplyCurve("FR", ((1.0, 0.0),), tech="nuclear")).equals(st)


def test_sans_courbe_le_bloc_unique_reste():
    """Sans observation on ne substitue pas une forme par défaut à un backtest — il doit rester mesurable."""
    st = pd.DataFrame({"unit_id": ["N"], "zone": "FR", "tech": ["nuclear"],
                       "capacity_mw": [1000.0], "min_gen_frac": [0.25]})
    assert expand_stack(st, None).equals(st)
