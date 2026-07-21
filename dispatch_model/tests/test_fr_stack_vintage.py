"""Stack FR millésimé : le parc doit être celui de l'année, ni plus ni moins.

Deux défauts corrigés, testés séparément parce qu'ils se compensaient partiellement en total tout en
faussant complètement la composition :
  - des centrales fermées depuis dix ans restaient au stack (charbon +156 %, fioul +134 %) ;
  - le parc diffus, non déclaré groupe par groupe, manquait entièrement (lac -75 %, gaz -33 %).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from dispatch_model.stacks.fr_stack import EFF_RANGE, FLEX, _topup_to_installed


class _Cfg:
    """Config minimale : `_topup_to_installed` ne consulte la base que via `installed_by_tech`."""
    def resolve(self, x):
        return x

    def section(self, _):
        return {"sqlite_path": ":memory:"}


def _stack(rows):
    return pd.DataFrame(rows, columns=["unit_id", "name", "tech", "capacity_mw", "efficiency",
                                       "min_gen_frac", "ramp_frac", "vom"])


def _patch_installed(monkeypatch, inst):
    import dispatch_model.io.fr_fleet as ff
    monkeypatch.setattr(ff, "installed_by_tech", lambda cfg, year: inst)


def test_ecart_comble_par_un_bloc_agrege(monkeypatch):
    _patch_installed(monkeypatch, {"hydro_reservoir": 8702.0})
    st = _stack([["FR_h", "lac", "hydro_reservoir", 2140.0, np.nan, 0.0, 1.0, 1.0]])
    out = _topup_to_installed(_Cfg(), st, 2024, np.random.default_rng(0))
    assert np.isclose(out["capacity_mw"].sum(), 8702.0)
    assert (out["unit_id"] == "FR_hydro_reservoir_diffus").sum() == 1


def test_pas_de_complement_si_le_parc_declare_suffit(monkeypatch):
    _patch_installed(monkeypatch, {"nuclear": 62990.0})
    st = _stack([["FR_n", "nuc", "nuclear", 62990.0, 0.33, 0.25, 0.05, 2.5]])
    out = _topup_to_installed(_Cfg(), st, 2024, np.random.default_rng(0))
    assert len(out) == 1                       # rien n'est ajouté


def test_ecart_negatif_ne_retire_rien(monkeypatch):
    """Un stack au-dessus de l'installé relève du filtre d'unités, pas du complément : on n'ampute pas."""
    _patch_installed(monkeypatch, {"coal": 1811.0})
    st = _stack([["FR_c", "chb", "coal", 4644.0, 0.38, 0.0, 0.4, 2.5]])
    out = _topup_to_installed(_Cfg(), st, 2024, np.random.default_rng(0))
    assert np.isclose(out["capacity_mw"].sum(), 4644.0)


def test_petit_ecart_ignore(monkeypatch):
    """Sous le seuil, l'écart est du bruit de réconciliation, pas un parc manquant."""
    _patch_installed(monkeypatch, {"coal": 1811.0})
    st = _stack([["FR_c", "chb", "coal", 1750.0, 0.38, 0.0, 0.4, 2.5]])
    out = _topup_to_installed(_Cfg(), st, 2024, np.random.default_rng(0))
    assert len(out) == 1


def test_bloc_diffus_prend_le_bas_de_la_bande_de_rendement(monkeypatch):
    """Les unités trop petites pour être déclarées sont les moins performantes : les placer au rendement
    médian les mettrait à tort trop bas dans l'ordre de mérite."""
    _patch_installed(monkeypatch, {"gas": 12720.0})
    st = _stack([["FR_g", "ccg", "gas", 8569.0, 0.55, 0.0, 1.0, 2.5]])
    out = _topup_to_installed(_Cfg(), st, 2024, np.random.default_rng(0))
    diffus = out[out["unit_id"] == "FR_gas_diffus"]
    assert np.isclose(float(diffus["efficiency"].iloc[0]), EFF_RANGE["gas"][0])


def test_bloc_diffus_herite_des_contraintes_de_la_filiere(monkeypatch):
    _patch_installed(monkeypatch, {"gas": 12720.0})
    st = _stack([["FR_g", "ccg", "gas", 8569.0, 0.55, 0.0, 1.0, 2.5]])
    out = _topup_to_installed(_Cfg(), st, 2024, np.random.default_rng(0))
    d = out[out["unit_id"] == "FR_gas_diffus"].iloc[0]
    assert np.isclose(d["min_gen_frac"], FLEX["gas"][0])
    assert np.isclose(d["ramp_frac"], FLEX["gas"][1])


def test_sans_installe_le_stack_est_inchange(monkeypatch):
    """Base sans capacités installées : on ne complète pas au jugé."""
    _patch_installed(monkeypatch, {})
    st = _stack([["FR_g", "ccg", "gas", 8569.0, 0.55, 0.0, 1.0, 2.5]])
    out = _topup_to_installed(_Cfg(), st, 2024, np.random.default_rng(0))
    assert out.equals(st)
