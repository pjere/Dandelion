"""Ré-allocation des NTC suisses : proportions physiques, **total inchangé**.

La NTC dérivée des flux capte correctement le total simultané d'une zone mais mal sa répartition entre
frontières. On corrige la répartition sans toucher au total — un simple plancher gonflait l'import
simultané de CH de 61 % au-dessus de tout ce qui a été observé, ce qui en faisait un nœud de transit non
physique. Tests sans base de données sur la fonction pure ; l'effet prix est couvert par le golden.

Depuis le split de DE_REST, CH a quatre frontières (FR / DE-LU / IT-North / AT_SI), la dernière n'existant
que grâce au cluster autrichien : les fixtures l'incluent pour vérifier qu'elle participe bien.
"""
from __future__ import annotations

import numpy as np
from dispatch_model.rolling.assemble import _NTC_FLOOR_ZONES, NTC, _apply_ntc_floor


def _legs(borders, direction):
    """(frontière, index) pour une direction vue depuis CH — 'imp' = vers CH, 'exp' = depuis CH."""
    if direction == "imp":
        return [(b, 0 if b[1] == "CH" else 1) for b in borders if "CH" in b]
    return [(b, 0 if b[0] == "CH" else 1) for b in borders if "CH" in b]


def _derived():
    """NTC dérivée plausible : DE→CH très sous-lue (960 contre ~4000 physiques), les autres compensant.
    Inclut CH↔AT_SI, la frontière ouverte par le split (§141)."""
    return {("FR", "CH"): (2318.0, 1515.0), ("DE_LU", "CH"): (960.0, 2333.0),
            ("CH", "IT_NORTH"): (3038.0, 2144.0), ("CH", "AT_SI"): (410.0, 880.0)}


def test_le_total_simultane_est_conserve():
    """Le cœur du correctif : la répartition change, le total non — c'est lui qui est physiquement borné."""
    d = _derived()
    out = _apply_ntc_floor(d)
    for direction in ("imp", "exp"):
        legs = _legs(d, direction)
        assert np.isclose(sum(out[b][i] for b, i in legs), sum(d[b][i] for b, i in legs))


def test_la_repartition_suit_les_proportions_physiques():
    """Après ré-allocation, deux directions entrantes sont dans le rapport de leurs capacités physiques."""
    out = _apply_ntc_floor(_derived())
    fr, de = out[("FR", "CH")][0], out[("DE_LU", "CH")][0]
    assert np.isclose(fr / de, NTC[("FR", "CH")][0] / NTC[("DE_LU", "CH")][0])


def test_la_frontiere_sous_lue_est_relevee():
    """DE→CH, lue à 960 MW pour une capacité physique de 4000, doit remonter nettement."""
    d = _derived()
    assert _apply_ntc_floor(d)[("DE_LU", "CH")][0] > 2 * d[("DE_LU", "CH")][0]


def test_la_frontiere_alpine_du_split_participe():
    """CH↔AT_SI (ouverte par le split) est ré-allouée à sa proportion physique, comme les autres."""
    out = _apply_ntc_floor(_derived())
    fr, at = out[("FR", "CH")][0], out[("CH", "AT_SI")][1]   # imports vers CH
    assert np.isclose(fr / at, NTC[("FR", "CH")][0] / NTC[("CH", "AT_SI")][1])


def test_les_frontieres_sans_ch_sont_intactes():
    d = {("FR", "DE_LU"): (100.0, 100.0), ("FR", "ES"): (50.0, 50.0)}
    assert _apply_ntc_floor(d) == d


def test_seule_la_suisse_est_reallouee():
    """Garde-fou : étendre la liste est un choix mesuré (cf. commentaire du module)."""
    assert _NTC_FLOOR_ZONES == frozenset({"CH"})
