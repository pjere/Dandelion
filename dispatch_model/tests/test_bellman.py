"""Valeur de l'eau structurelle : la SDP doit reproduire les propriétés économiques attendues.

Les tests portent sur le *comportement* de λ_t(S), pas sur des valeurs numériques arbitraires : une
fonction valeur correcte doit être concave en stock, sa dérivée décroissante, et s'effondrer quand le
réservoir est plein (le déversement rend l'eau sans valeur).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from dispatch_model.hydro.bellman import (
    HOURS_PER_WEEK,
    Reservoir,
    infer_inflows,
    revenue_curve,
    solve,
    water_value_at,
)

RES = Reservoir(zone="FR", s_min=0.0, s_max=1_000_000.0, p_max_mw=2_000.0)


def _prices(mean, spread=20.0, n=HOURS_PER_WEEK, seed=0):
    rng = np.random.default_rng(seed)
    return mean + spread * rng.standard_normal(n)


def _flat_inflows(mwh, weeks=None):
    w = list(weeks if weeks is not None else range(1, 53))
    return pd.DataFrame({"week": w, "inflow_mwh": [mwh] * len(w)})


def test_revenu_est_concave():
    """L'eau va d'abord aux heures chères : le revenu marginal décroît. C'est ce qui étale la production."""
    u = np.linspace(0, RES.u_max, 50)
    r = revenue_curve(_prices(60.0), RES.p_max_mw, u)
    d = np.diff(r) / np.diff(u)
    assert np.all(np.diff(d) <= 1e-6)


def test_revenu_croissant_et_nul_en_zero():
    u = np.linspace(0, RES.u_max, 30)
    r = revenue_curve(_prices(60.0), RES.p_max_mw, u)
    assert np.isclose(r[0], 0.0) and np.all(np.diff(r) >= -1e-9)


def test_valeur_de_l_eau_decroit_avec_le_stock():
    """Concavité de V : plus le réservoir est plein, moins le MWh marginal vaut."""
    wp = {w: _prices(60.0, seed=w) for w in range(1, 53)}
    sol = solve(RES, wp, _flat_inflows(200_000.0), n_storage=25, n_release=25, max_sweeps=6)
    lam = sol["water_value"][0]
    # tendance décroissante (le bruit de grille peut produire de petites remontées locales)
    assert np.polyfit(sol["storage_mwh"], lam, 1)[0] < 0


def test_valeur_s_effondre_quand_le_reservoir_est_plein():
    """Réservoir plein + apports abondants : l'eau va déverser, elle ne vaut plus rien."""
    wp = {w: _prices(60.0, seed=w) for w in range(1, 53)}
    sol = solve(RES, wp, _flat_inflows(0.9 * RES.u_max), n_storage=25, n_release=25, max_sweeps=6)
    lam = sol["water_value"][0]
    assert lam[-1] < lam[0]


def test_prix_eleves_donnent_une_eau_plus_chere():
    """La valeur de l'eau est un coût d'opportunité : elle suit le niveau des prix futurs."""
    inf = _flat_inflows(150_000.0)
    bas = solve(RES, {w: _prices(30.0, seed=w) for w in range(1, 53)}, inf,
                n_storage=25, n_release=25, max_sweeps=6)
    haut = solve(RES, {w: _prices(120.0, seed=w) for w in range(1, 53)}, inf,
                 n_storage=25, n_release=25, max_sweeps=6)
    s = 0.5 * RES.s_max
    assert water_value_at(haut, 1, s) > water_value_at(bas, 1, s)


def test_penurie_d_apports_renforce_la_valeur():
    """Peu d'eau attendue ⇒ chaque MWh stocké est plus précieux."""
    wp = {w: _prices(60.0, seed=w) for w in range(1, 53)}
    sec = solve(RES, wp, _flat_inflows(20_000.0), n_storage=25, n_release=25, max_sweeps=6)
    humide = solve(RES, wp, _flat_inflows(400_000.0), n_storage=25, n_release=25, max_sweeps=6)
    s = 0.5 * RES.s_max
    assert water_value_at(sec, 1, s) > water_value_at(humide, 1, s)


def test_saisonnalite_apparait():
    """Prix d'hiver élevés, prix d'été bas : la valeur de l'eau doit varier dans l'année."""
    wp = {w: _prices(100.0 if (w <= 8 or w >= 45) else 35.0, seed=w) for w in range(1, 53)}
    sol = solve(RES, wp, _flat_inflows(150_000.0), n_storage=25, n_release=25, max_sweeps=8)
    s = 0.5 * RES.s_max
    lam = [water_value_at(sol, w, s) for w in range(1, 53)]
    assert max(lam) - min(lam) > 1.0


def test_apports_inferes_par_bilan():
    idx = pd.date_range("2024-01-07", periods=4, freq="7D", tz="UTC")
    stock = pd.Series([500_000.0, 600_000.0, 550_000.0, 500_000.0], index=idx)
    gen = pd.Series([50_000.0] * 4, index=idx)
    out = infer_inflows(stock, gen, s_max=1_000_000.0, smooth_weeks=1)
    # apport = ΔStock + production : (600-500)+50 = 150k, puis (550-600)+50 = 0, puis (500-550)+50 = 0
    assert np.isclose(out["inflow_mwh"].iloc[0], 150_000.0)
    assert (out["inflow_mwh"] >= 0).all()


def test_base_de_production_large_augmente_les_apports():
    """Le vrai correctif du biais : compter TOUTE l'eau soutirée (STEP comprises). N'en compter qu'une
    partie sous-estime les apports, donc rend l'eau trop chère."""
    idx = pd.date_range("2024-01-07", periods=4, freq="7D", tz="UTC")
    stock = pd.Series([500e3, 520e3, 540e3, 560e3], index=idx)
    lac = pd.Series([60e3] * 4, index=idx)
    lac_et_step = lac + 25e3
    etroit = infer_inflows(stock, lac, s_max=2e6)["inflow_mwh"].sum()
    large = infer_inflows(stock, lac_et_step, s_max=2e6)["inflow_mwh"].sum()
    assert large > etroit


def test_ecretage_gonfle_les_apports():
    """Sens du biais, contre-intuitif : écrêter à zéro REMONTE les bilans négatifs, donc gonfle le total.
    Lisser avant écrêtage les réduit — c'est pourquoi le lissage n'est pas activé par défaut."""
    idx = pd.date_range("2024-01-07", periods=6, freq="7D", tz="UTC")
    stock = pd.Series([500e3, 700e3, 500e3, 700e3, 500e3, 700e3], index=idx)
    gen = pd.Series([100e3] * 6, index=idx)
    brut = infer_inflows(stock, gen, s_max=2e6, smooth_weeks=1)["inflow_mwh"].sum()
    lisse = infer_inflows(stock, gen, s_max=2e6, smooth_weeks=3)["inflow_mwh"].sum()
    assert brut > lisse


def test_debit_reserve_force_le_turbinage():
    """Le débit réservé est physique : on turbine même quand c'est économiquement absurde."""
    wp = {w: _prices(5.0, spread=1.0, seed=w) for w in range(1, 53)}     # prix très bas partout
    sans = solve(RES, wp, _flat_inflows(150_000.0), n_storage=25, n_release=25, max_sweeps=8)
    avec = solve(Reservoir("FR", RES.s_min, RES.s_max, RES.p_max_mw, min_release_mw=400.0),
                 wp, _flat_inflows(150_000.0), n_storage=25, n_release=25, max_sweeps=8)
    # contraint de turbiner, le stock se vide : l'eau restante devient plus précieuse
    assert water_value_at(avec, 1, 0.5 * RES.s_max) >= water_value_at(sans, 1, 0.5 * RES.s_max) - 1e-9


def test_debit_reserve_cede_devant_la_penurie():
    """On ne turbine pas une eau qu'on n'a pas : le plancher ne doit pas rendre le problème infaisable."""
    r = Reservoir("FR", 0.0, 100_000.0, 2_000.0, min_release_mw=2_000.0)   # plancher = pleine puissance
    sol = solve(r, {w: _prices(50.0, seed=w) for w in range(1, 53)},
                _flat_inflows(0.0), n_storage=15, n_release=15, max_sweeps=5)
    assert np.all(np.isfinite(sol["water_value"]))


def test_semaines_a_reservoir_plein_ecartees():
    """Le déversement n'est pas publié : à réservoir plein le bilan sous-estime les apports."""
    idx = pd.date_range("2024-01-07", periods=3, freq="7D", tz="UTC")
    stock = pd.Series([995_000.0, 990_000.0, 500_000.0], index=idx)   # 2 semaines au plafond
    out = infer_inflows(stock, pd.Series([0.0] * 3, index=idx), s_max=1_000_000.0)
    assert len(out) < 2


def test_convergence_du_point_fixe_cyclique():
    wp = {w: _prices(60.0, seed=w) for w in range(1, 53)}
    sol = solve(RES, wp, _flat_inflows(150_000.0), n_storage=25, n_release=25, max_sweeps=40)
    assert sol["converged"], f"non convergé en {sol['sweeps']} balayages"
