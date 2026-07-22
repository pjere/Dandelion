"""Synthèse SDP × courbe empirique : le niveau vient de Bellman, la dispersion de l'observation.

Deux modèles de valeur de l'eau coexistaient sans se parler :

- `hydro.water_value` — la **courbe empirique** (préférence révélée). Elle capture la *dispersion* réelle
  du parc (un ensemble de retenues aux coûts d'opportunité différents, d'où une courbe et non un scalaire)
  mais son *niveau* est circulaire : calibré sur les prix observés pour reproduire les prix du modèle.
- `hydro.bellman` — la **SDP structurelle**. Elle donne un niveau λ_t(S) non circulaire, qui dépend du
  stock et de la saison (élevé en fin d'hiver à stock bas, faible à la fonte), mais son réservoir
  équivalent agrégé écrase l'hétérogénéité du parc — elle ne produit qu'un scalaire par semaine.

Chacun a ce qui manque à l'autre. La synthèse recentre la courbe empirique pour que sa valeur d'eau
moyenne (tranches arbitrées, hors débit réservé et hors tranche de rareté) égale λ_t(S_t) de la SDP à la
semaine et au stock courants :

    prix d'offre de la tranche i  =  λ_t(S_t)  +  (valeur empirique_i − moyenne empirique)

Le décalage est **additif et uniforme**, donc la monotonie de la courbe est préservée. Le débit réservé
(première tranche, offerte sous zéro) et la tranche de rareté restent des ancres, non déplacées : l'un est
physique, l'autre est une hypothèse hors du domaine observé.

Mesuré (backtest, |erreur baseload| moyenne 4 zones hydro) : 2024 25,55 → 23,32, 2019 8,40 → 8,28. Le gain
vient surtout de l'Espagne, dont la SDP price l'eau à 121 €/MWh là où l'empirique disait 28 — et 121 est
plus proche du vrai. La métrique d'utilisation `P(prix>λ)` faisait pourtant voir ce λ haut comme un défaut ;
le backtest prix, qui est l'objectif, tranche l'inverse.

Le flag est `hydro_sdp_level`, `False` par défaut : le chemin de production et le golden ne changent pas.
"""
from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

from .water_value import SCARCITY_WV, HydroCurve

_VOM = 1.0          # ajouté par expand_stack aux prix d'offre hydrauliques (cf. water_value.tranche_rows)

# Le stock et la production sur 11 ans ne dépendent PAS de l'année de backtest : on les mémorise par zone
# pour qu'un golden (2019 + 2024) ne relise pas deux fois 88 années de génération. Clé = (id(config), zone).
_STOCK_CACHE: dict[tuple, pd.Series] = {}
_GEN_CACHE: dict[tuple, pd.Series] = {}


def _stock_series(config, zone: str) -> pd.Series:
    """Stock hebdomadaire observé (MWh) : `rte_water_reserves` pour FR, `entsoe_hydro_storage` sinon."""
    key = (id(config), zone)
    if key in _STOCK_CACHE:
        return _STOCK_CACHE[key]
    con = sqlite3.connect(config.resolve(config.section("data")["sqlite_path"]))
    try:
        if zone == "FR":
            df = pd.read_sql("SELECT ts_utc, value FROM rte_water_reserves ORDER BY ts_utc", con)
        else:
            df = pd.read_sql("SELECT ts_utc, value FROM entsoe_hydro_storage WHERE series_key = ? "
                             "ORDER BY ts_utc", con, params=(zone,))
    except Exception:                                            # noqa: BLE001 — table absente : pas de SDP
        return pd.Series(dtype=float)
    finally:
        con.close()
    if df.empty:
        _STOCK_CACHE[key] = pd.Series(dtype=float)
        return _STOCK_CACHE[key]
    s = df.assign(ts=pd.to_datetime(df["ts_utc"], utc=True, format="mixed")).set_index("ts")["value"]
    s = s.astype(float).sort_index()
    _STOCK_CACHE[key] = s[s.index.year.isin(range(2015, 2026))]
    return _STOCK_CACHE[key]


def _broad_generation(config, zone: str) -> pd.Series:
    """Toute la production tirant sur la retenue (lac + STEP), toutes années — base des apports inférés."""
    key = (id(config), zone)
    if key in _GEN_CACHE:
        return _GEN_CACHE[key]
    from ..io.entsoe_hist import load_generation_hist
    from ..neighbours.blocks import constituents
    out = []
    for y in range(2015, 2026):
        g = load_generation_hist(config, y, zones=constituents(zone))
        if g.empty:
            continue
        p = g.pivot_table(index="timestamp_utc", columns="tech", values="gen_mw", aggfunc="sum")
        cols = [t for t in ("hydro_reservoir", "hydro_psp") if t in p.columns]
        if cols:
            out.append(p[cols].sum(axis=1))
    _GEN_CACHE[key] = pd.concat(out).sort_index() if out else pd.Series(dtype=float)
    return _GEN_CACHE[key]


def _reservoir_inputs(config, year: int, zone: str):
    """(`Reservoir`, apports, prix horaires par semaine, stock de l'année) ou `None` si un ingrédient manque.

    Le débit réservé est calibré au 5ᵉ centile de la production de lac observée — une contrainte physique
    (obligations de débit, irrigation) que l'arbitrage pur ne produit pas. `s_min`/`s_max` = extrêmes
    observés du stock. Les apports sont inférés par bilan sur la production large (lac + STEP)."""
    from ..io.entsoe_hist import load_generation_hist
    from ..io.fr_history import load_fr_netload
    from ..neighbours.blocks import build_neighbour_stack, constituents
    from ..rolling.backtest import _observed_prices
    from ..rolling.windows import fr_stack_base
    from .bellman import Reservoir, infer_inflows

    sk = _stock_series(config, zone)
    if sk.empty or sk.max() <= sk.min():
        return None
    try:
        st = fr_stack_base(config, year) if zone == "FR" else build_neighbour_stack(config, zone, year)
    except (KeyError, ValueError):
        return None
    pmax = float(st.loc[st["tech"] == "hydro_reservoir", "capacity_mw"].sum())
    if pmax <= 0:
        return None
    if zone == "FR":
        lac = load_fr_netload(config, f"{year}-01-01", f"{year + 1}-01-01") \
            .set_index("timestamp_utc").get("gen_hydro_reservoir_mw")
    else:
        g = load_generation_hist(config, year, zones=constituents(zone))
        piv = g.pivot_table(index="timestamp_utc", columns="tech", values="gen_mw", aggfunc="sum")
        lac = piv.get("hydro_reservoir")
    if lac is None or lac.dropna().empty:
        return None
    min_release = float(np.nanquantile(lac.to_numpy(float) / pmax, 0.05) * pmax)
    wg = _broad_generation(config, zone).resample("7D", origin=sk.index[0]).sum() \
        .reindex(sk.index, method="nearest")
    inflows = infer_inflows(sk, wg, float(sk.max()))
    o = _observed_prices(config, year, [zone]).get(zone)
    if o is None:
        return None
    o = o.dropna()
    wkn = pd.DatetimeIndex(o.index).isocalendar().week.astype(int)
    weekly_prices = {int(w): o[wkn == w].to_numpy(float)
                     for w in sorted(set(wkn)) if (wkn == w).sum() >= 24}
    res = Reservoir(zone, float(sk.min()), float(sk.max()), pmax, min_release_mw=min_release)
    return res, inflows, weekly_prices, sk[sk.index.year == year]


def _empirical_level(curve: HydroCurve) -> float | None:
    """Valeur d'eau moyenne des tranches **arbitrées** : hors débit réservé (1re) et hors rareté."""
    core = [(s, w) for i, (s, w) in enumerate(curve.tranches)
            if i != 0 and abs(w - SCARCITY_WV) > 1e-6]
    if not core:
        return None
    return sum(s * w for s, w in core) / sum(s for s, w in core)


def _weekly_levels(config, year: int, zone: str, curve: HydroCurve) -> dict[int, float] | None:
    """{semaine ISO : décalage à appliquer aux prix d'offre hydrauliques} = λ_t(S_t) − niveau empirique.

    Résout la SDP de la zone sur les prix et apports de l'année, puis évalue λ à la trajectoire de stock
    réelle, semaine par semaine. Renvoie `None` si un ingrédient manque (stock, prix ou capacité absents).
    """
    wbar = _empirical_level(curve)
    if wbar is None:
        return None
    bundle = _reservoir_inputs(config, year, zone)
    if bundle is None:
        return None
    from .bellman import solve, water_value_at
    res, inflows, weekly_prices, stock_year = bundle
    if not weekly_prices:
        return None
    sol = solve(res, weekly_prices, inflows)
    out = {}
    for ts, s_obs in stock_year.items():
        w = int(pd.Timestamp(ts).isocalendar().week)
        if w in weekly_prices:
            out[w] = float(water_value_at(sol, w, float(s_obs)) - wbar)
    return out or None


def solve_levels(config, year: int, curves: dict[str, HydroCurve],
                 zones: tuple[str, ...]) -> dict[str, dict[int, float]]:
    """Par zone hydro : {semaine ISO → décalage de niveau}. Zones sans SDP exploitable omises."""
    out = {}
    for z in zones:
        c = curves.get(z)
        if c is None:
            continue
        lv = _weekly_levels(config, year, z, c)
        if lv:
            out[z] = lv
    return out


def shift_hydro_bids(stack: pd.DataFrame, delta: float, bid_col: str = "srmc_eur_mwh",
                     tech: str = "hydro_reservoir") -> pd.DataFrame:
    """Décale les prix d'offre des tranches hydrauliques **arbitrées** de `delta` (recentrage sur λ).

    Le débit réservé (tranche la moins chère) et la tranche de rareté (`SCARCITY_WV`) sont des ancres, non
    déplacées. Le décalage est borné pour rester strictement au-dessus du débit réservé et strictement
    sous la rareté, ce qui préserve la monotonie de la courbe même pour un `delta` extrême.
    """
    if not np.isfinite(delta) or delta == 0.0 or bid_col not in stack.columns:
        return stack
    m = (stack["tech"] == tech).to_numpy()
    if m.sum() <= 1:
        return stack
    bids = stack[bid_col].to_numpy(float)
    hy = np.where(m)[0]
    lo = hy[np.argmin(bids[hy])]                                  # débit réservé : ancre basse
    floor = bids[lo] + 0.01
    ceil = SCARCITY_WV + _VOM - 0.01
    out = stack.copy()
    col = out[bid_col].to_numpy(float)
    for i in hy:
        if i == lo or abs(bids[i] - (SCARCITY_WV + _VOM)) < 1e-6:   # ancres : débit réservé / rareté
            continue
        col[i] = min(max(bids[i] + delta, floor), ceil)
    out[bid_col] = col
    return out
