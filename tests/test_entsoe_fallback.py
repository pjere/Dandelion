"""Repli ENTSO-E dans build_master : corriger les trous RTE sans écraser ce qui est correct.

Le risque n'est pas de rater une correction — c'est d'en faire une de trop. Une règle trop large
remplacerait des données RTE valides par une source de convention différente, ce qui est plus difficile à
détecter qu'un trou franc.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from pricemodeling.merge.build_master import (
    ENTSOE_FALLBACK,
    FALLBACK_FLOOR_MW,
    FALLBACK_REL,
    _apply_entsoe_fallback,
)


def _frames(rte_vals, ent_vals, col="prod_nuclear"):
    idx = pd.date_range("2024-09-01", periods=len(rte_vals), freq="h", tz="UTC")
    return pd.DataFrame({col: rte_vals}, index=idx), pd.DataFrame({col: ent_vals}, index=idx)


def test_zero_hours_are_replaced():
    m, e = _frames([0.0, 0.0, 0.0], [40000.0, 40000.0, 40000.0])
    out, rep = _apply_entsoe_fallback(m, e)
    assert out["prod_nuclear"].tolist() == [40000.0] * 3
    assert rep["prod_nuclear"] == 3


def test_depressed_hours_are_replaced():
    """26 GW contre 40 GW : le creux de publication de sept/oct 2024."""
    m, e = _frames([26000.0], [40000.0])
    out, rep = _apply_entsoe_fallback(m, e)
    assert out["prod_nuclear"].iloc[0] == 40000.0
    assert rep["prod_nuclear"] == 1


def test_missing_rte_is_filled():
    m, e = _frames([np.nan], [38000.0])
    out, rep = _apply_entsoe_fallback(m, e)
    assert out["prod_nuclear"].iloc[0] == 38000.0
    assert rep["prod_nuclear"] == 1


def test_agreeing_hours_are_left_alone():
    """Le cas dominant : 911 des 1463 heures examinées concordent à moins de 2 %."""
    m, e = _frames([38210.0, 45653.0], [38200.0, 45700.0])
    out, rep = _apply_entsoe_fallback(m.copy(), e)
    assert out["prod_nuclear"].tolist() == [38210.0, 45653.0]
    assert "prod_nuclear" not in rep


def test_small_deviation_is_not_substituted():
    """Juste au-dessus du seuil : un écart de méthode, pas un défaut — on ne touche pas."""
    ent = 10000.0
    m, e = _frames([FALLBACK_REL * ent + 1], [ent])
    out, rep = _apply_entsoe_fallback(m.copy(), e)
    assert out["prod_nuclear"].iloc[0] == FALLBACK_REL * ent + 1
    assert "prod_nuclear" not in rep


def test_small_technologies_are_protected_by_the_floor():
    """Sous le plancher, l'écart relatif est du bruit : substituer y ferait plus de mal que de bien."""
    m, e = _frames([0.0], [FALLBACK_FLOOR_MW - 1])
    out, rep = _apply_entsoe_fallback(m.copy(), e)
    assert out["prod_nuclear"].iloc[0] == 0.0
    assert rep == {}


def test_pumped_storage_is_never_substituted():
    """RTE publie le pompage en net, ENTSO-E en brut : divergence permanente et légitime."""
    assert "prod_hydro_pumped_storage" not in ENTSOE_FALLBACK
    m, e = _frames([-250.0], [1450.0], col="prod_hydro_pumped_storage")
    out, rep = _apply_entsoe_fallback(m.copy(), e)
    assert out["prod_hydro_pumped_storage"].iloc[0] == -250.0
    assert rep == {}


def test_missing_entsoe_never_destroys_rte():
    """Sans contrepartie ENTSO-E, RTE fait foi — un trou côté ENTSO-E ne doit rien effacer."""
    m, e = _frames([30000.0], [np.nan])
    out, rep = _apply_entsoe_fallback(m.copy(), e)
    assert out["prod_nuclear"].iloc[0] == 30000.0
    assert rep == {}


def test_absent_column_is_skipped_cleanly():
    idx = pd.date_range("2024-09-01", periods=2, freq="h", tz="UTC")
    m = pd.DataFrame({"prod_nuclear": [1000.0, 1000.0]}, index=idx)
    out, rep = _apply_entsoe_fallback(m.copy(), pd.DataFrame(index=idx))
    assert out["prod_nuclear"].tolist() == [1000.0, 1000.0]
    assert rep == {}
