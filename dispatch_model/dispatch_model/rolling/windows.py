"""Shared per-window LP input assembly for the backtest AND the projection.

Both engines clear the same weekly windows — the backtest against a historical year, the projection
against reference-year shapes evolved to a target year — so the stack/availability/DSR assembly lives
here once. `fr_window` builds the FR unit-level zone dict (availability proxy or REMIT feed, DSR
tranches, hydro budget); `nb_window` the aggregated neighbour block dict (measured CHP must-run,
reservoir budget); `fr_stack_base` the FR stack with the GB border-import tranches appended.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import xarray as xr

from ..hydro.water_value import apply_water_value
from ..neighbours.blocks import heat_factor, measured_chp_mw
from ..stacks.fr_stack import build_fr_stack, srmc
from .assemble import _EXCLUDE_DISPATCH

# GB_IMPORT_TRANCHES lived here — 2500 MW at 52 EUR/MWh + 1500 MW at 110 EUR/MWh of "import" supply
# appended to the FR stack, standing in for the Channel interconnectors while GB had no ENTSO-E data and
# so no balance of its own. REMOVED when GB was promoted to a modelled zone: their 4000 MW is exactly the
# FR-GB NTC now in `assemble.NTC`, so keeping both would let France draw 4000 MW of phantom British supply
# at a fixed price AND 4000 MW of real flow across the border — 8 GW of import capacity where 4 exists,
# half of it at a price no British plant had to set.

# DSR / scarcity tranches as a fraction of window peak demand (spec §2: step the price below VoLL).
# Also absorbs modest under-modelling of peakers/emergency imports so cold snaps don't hit VoLL.
#: Demand-response ladder: (fraction of the zone's peak demand, bid €/MWh). Three rungs, 11 % of peak.
#:
#: Lowered from 300/1000/4000 (2026-08). The old top rung was the model's de-facto scarcity price once
#: adequacy closed: measured on FR 2046 with the flexibility module on, the SMC sat at exactly 4000 for
#: **170 hours**, which alone carried 79 €/MWh of a 221 €/MWh annual mean. A ladder written to represent
#: emergency load-shedding was setting the average price of the year.
#:
#: 180/250/500 puts the ladder in the same economic register as the resources it competes with — the
#: DSR/H2 block bids `VOM["flex"]` = 180 — so scarcity now clears through a plausible willingness-to-pay
#: rather than through a number chosen as a stand-in for VoLL.
#:
#: NOTE the first rung equals `VOM["flex"]` exactly, as 300 did before. Two independent scarcity
#: parameters again sit on the same value, so a sensitivity on one alone will find ~40 % of the affected
#: hours pinned by the other (measured on the 300 pair: of 3140 hours at 300, 1324 did not move when the
#: flex bid alone was lowered). Move both together.
_DSR = [(0.03, 180.0), (0.03, 250.0), (0.05, 500.0)]


def fr_stack_base(config, year: int | None = None) -> pd.DataFrame:
    """FR unit-level stack, dispatchables only.

    `year` sélectionne le parc de l'année : unités réellement en service, et complément agrégé pour le
    parc diffus absent du reporting groupe par groupe (voir `io.fr_fleet`).
    """
    st = build_fr_stack(config, year=year)
    return st[~st["tech"].isin(_EXCLUDE_DISPATCH)].reset_index(drop=True)


def dsr_tranches(zone: str, peak_mw: float) -> pd.DataFrame:
    return pd.DataFrame([{"unit_id": f"{zone}_DSR{i}", "name": "dsr", "tech": "dsr",
                          "capacity_mw": frac * peak_mw, "min_gen_frac": 0.0, "efficiency": np.nan,
                          "ramp_frac": 1.0, "vom": 0.0, "srmc_eur_mwh": price}
                         for i, (frac, price) in enumerate(_DSR)])


def fr_window(fr, stack, prices, T, nuc_unavail_daily=None, wv_delta=None) -> dict:
    """FR zone dict for one window: SRMC, DSR tranches, nuclear availability (REMIT feed or the
    rolling-max-of-output proxy), and the window's actual reservoir energy as the hydro budget.

    `wv_delta` (opt-in, #136) recentre les prix d'offre hydrauliques sur le λ structurel de la SDP pour la
    semaine de la fenêtre — le niveau vient de Bellman, la dispersion reste empirique."""
    h = fr.loc[T]
    # la valeur de l'eau ecrase le SRMC des tranches hydrauliques : leur cout d'opportunite, pas leur VOM
    st = apply_water_value(stack.assign(srmc_eur_mwh=srmc(stack, prices).to_numpy()))
    if wv_delta is not None:
        from ..hydro.synthesis import shift_hydro_bids
        st = shift_hydro_bids(st, float(wv_delta))
    st = pd.concat([st, dsr_tranches("FR", float(h["demand_mw"].max()))], ignore_index=True)
    nuc_cap = st.loc[st["tech"] == "nuclear", "capacity_mw"].sum()
    if nuc_unavail_daily is not None:
        # step-vi feed (#78): true REMIT nuclear availability = 1 − outage_MW / installed, broadcast day→hour
        days = pd.DatetimeIndex(T).normalize().tz_localize(None).date
        un = pd.Series(days).map(nuc_unavail_daily).fillna(0.0).to_numpy()
        nuc_frac = np.clip(1.0 - un / max(nuc_cap, 1), 0, 1)
    else:                                                        # default proxy: rolling max of output
        nuc_frac = np.clip(h["gen_nuclear_mw"].rolling(72, 1).max().to_numpy() / max(nuc_cap, 1) * 1.03, 0, 1)
    frac = {"nuclear": nuc_frac, "gas": 0.95, "coal": 0.95, "oil": 0.95, "biomass": 0.85,
            "hydro_reservoir": 1.0, "import": 1.0, "dsr": 1.0}
    av = np.ones((len(st), len(T)))
    for i, t in enumerate(st["tech"]):
        av[i, :] = frac.get(t, 0.9)                              # scalar or per-hour array both broadcast
    avail = xr.DataArray(av, coords=[("unit", st["unit_id"].to_numpy()), ("time", T)])
    return {"stack": st, "demand": h["demand_mw"].to_numpy(), "res_pot": h["musttake_res_mw"].to_numpy(),
            "avail": avail, "energy_caps": {"hydro_reservoir": float(h["gen_hydro_reservoir_mw"].sum())}}


def apply_measured_mustrun(st, zone, T, floors: dict | None = None) -> pd.DataFrame:
    """Where the reference registry has measured CHP for `zone`, replace the workbook must-run fractions
    with chp_el(tech) × heat_factor(month): the heat-obligated floor, seasonal not flat. Sub-blocks of a
    tech sum to its capacity, so a common per-block fraction ⇒ forced = frac × cap = chp × heat_factor.

    `floors` (opt-in, FLEX): {month: {tech: MW}} MEASURED floors (p10 of observed generation —
    `blocks.observed_mustrun_floors`) that REPLACE the chp×heat_factor heuristic, which overstates the
    heat-obligated minimum ~5× for flexible gas CHP (heat storage decouples heat from power)."""
    month = pd.DatetimeIndex(T)[len(T) // 2].month               # window's central month
    if floors is not None:
        fl = floors.get(int(month), {})
        st = st.copy()
        for tech, mw in fl.items():
            rows = st["tech"] == tech
            cap = st.loc[rows, "capacity_mw"].sum()
            if cap > 0:
                st.loc[rows, "min_gen_frac"] = min(1.0, float(mw) / cap)
        return st
    chp = measured_chp_mw(zone)
    if not chp:
        return st
    hf = heat_factor(month)
    st = st.copy()
    for tech, chp_mw in chp.items():
        rows = st["tech"] == tech
        cap = st.loc[rows, "capacity_mw"].sum()
        if cap > 0:
            st.loc[rows, "min_gen_frac"] = min(1.0, chp_mw * hf / cap)
    return st


def nb_window(zone, stack, nl, res, prices, T, wv_delta=None, mustrun_floors=None,
              avail_profile=None) -> dict:
    """Neighbour zone dict for one window: block SRMC, measured seasonal must-run (DE_LU), DSR tranches,
    reservoir budget from the window's actual generation.

    `wv_delta` (opt-in, #136) recentre les prix d'offre hydrauliques sur le λ structurel de la SDP.
    `mustrun_floors` (opt-in, FLEX) : planchers must-run MESURÉS {month: {tech: MW}} remplaçant
    l'heuristique chp×heat_factor (cf. apply_measured_mustrun).

    `avail_profile` (opt-in) : {tech: {mois: fraction}} de `blocks.monthly_avail` — la forme SAISONNIÈRE
    de disponibilité thermique. Sans lui les voisins passent `avail=None`, donc 1.0 partout, et le modèle
    peut dépenser en juin une disponibilité de novembre : mesuré sur le charbon+lignite allemand, 7 à 12 GW
    ne sont jamais livrés même aux 100 heures les plus chères de l'année, et ce dans CHAQUE année. Les
    techs absentes du profil gardent 1.0, y compris les tranches DSR ajoutées plus bas."""
    st = apply_water_value(stack.assign(srmc_eur_mwh=srmc(stack, prices).to_numpy()))
    if wv_delta is not None:
        from ..hydro.synthesis import shift_hydro_bids
        st = shift_hydro_bids(st, float(wv_delta))
    st = apply_measured_mustrun(st, zone, T, floors=mustrun_floors)   # DE_LU: measured seasonal must-run
    w = nl.reindex(T).interpolate().ffill().bfill()
    st = pd.concat([st, dsr_tranches(zone, float(w["load_mw"].max()))], ignore_index=True)
    budget = float(res.reindex(T).fillna(0).sum()) if len(res) else 0.0
    caps = {"hydro_reservoir": budget} if budget > 0 and (st["tech"] == "hydro_reservoir").any() else {}
    av = None
    if avail_profile:
        months = pd.DatetimeIndex(T).month
        a = np.ones((len(st), len(T)))
        for i, tech in enumerate(st["tech"].to_numpy()):
            prof = avail_profile.get(tech)
            if prof:
                a[i, :] = [prof.get(int(m), 1.0) for m in months]
        av = xr.DataArray(a, coords=[("unit", st["unit_id"].to_numpy()), ("time", T)])
    return {"stack": st, "demand": w["load_mw"].to_numpy(), "res_pot": w["musttake_res_mw"].to_numpy(),
            "avail": av, "energy_caps": caps}
