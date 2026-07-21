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

# GB is not on ENTSO-E (post-Brexit) → the GB interconnector is two border supply tranches on the FR
# stack: unit_id -> (capacity_mw, srmc_eur_mwh). Priced by unit_id (never by row position).
GB_IMPORT_TRANCHES = {"GB_IMP1": (2500.0, 52.0), "GB_IMP2": (1500.0, 110.0)}

# DSR / scarcity tranches as a fraction of window peak demand (spec §2: step the price below VoLL).
# Also absorbs modest under-modelling of peakers/emergency imports so cold snaps don't hit VoLL.
_DSR = [(0.03, 300.0), (0.03, 1000.0), (0.05, 4000.0)]


def fr_stack_base(config, year: int | None = None) -> pd.DataFrame:
    """FR unit-level stack (dispatchables only) + the GB border-import tranches.

    `year` sélectionne le parc de l'année : unités réellement en service, et complément agrégé pour le
    parc diffus absent du reporting groupe par groupe (voir `io.fr_fleet`).
    """
    st = build_fr_stack(config, year=year)
    st = st[~st["tech"].isin(_EXCLUDE_DISPATCH)].reset_index(drop=True)
    gb = pd.DataFrame([{"unit_id": uid, "name": uid, "tech": "import", "capacity_mw": cap,
                        "min_gen_frac": 0.0, "efficiency": np.nan, "ramp_frac": 1.0, "vom": 0.0}
                       for uid, (cap, _) in GB_IMPORT_TRANCHES.items()])
    return pd.concat([st, gb], ignore_index=True)


def dsr_tranches(zone: str, peak_mw: float) -> pd.DataFrame:
    return pd.DataFrame([{"unit_id": f"{zone}_DSR{i}", "name": "dsr", "tech": "dsr",
                          "capacity_mw": frac * peak_mw, "min_gen_frac": 0.0, "efficiency": np.nan,
                          "ramp_frac": 1.0, "vom": 0.0, "srmc_eur_mwh": price}
                         for i, (frac, price) in enumerate(_DSR)])


def price_gb_tranches(stack: pd.DataFrame, srmc_values: np.ndarray) -> np.ndarray:
    """Overwrite the GB import tranches' SRMC by unit_id — robust to any stack reordering."""
    s = srmc_values.copy()
    for uid, (_, price) in GB_IMPORT_TRANCHES.items():
        s[(stack["unit_id"] == uid).to_numpy()] = price
    return s


def fr_window(fr, stack, prices, T, nuc_unavail_daily=None) -> dict:
    """FR zone dict for one window: SRMC, DSR tranches, nuclear availability (REMIT feed or the
    rolling-max-of-output proxy), and the window's actual reservoir energy as the hydro budget."""
    h = fr.loc[T]
    s = price_gb_tranches(stack, srmc(stack, prices).to_numpy())
    # la valeur de l'eau ecrase le SRMC des tranches hydrauliques : leur cout d'opportunite, pas leur VOM
    st = apply_water_value(stack.assign(srmc_eur_mwh=s))
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


def apply_measured_mustrun(st, zone, T) -> pd.DataFrame:
    """Where the reference registry has measured CHP for `zone`, replace the workbook must-run fractions
    with chp_el(tech) × heat_factor(month): the heat-obligated floor, seasonal not flat. Sub-blocks of a
    tech sum to its capacity, so a common per-block fraction ⇒ forced = frac × cap = chp × heat_factor."""
    chp = measured_chp_mw(zone)
    if not chp:
        return st
    hf = heat_factor(pd.DatetimeIndex(T)[len(T) // 2].month)     # window's central month
    st = st.copy()
    for tech, chp_mw in chp.items():
        rows = st["tech"] == tech
        cap = st.loc[rows, "capacity_mw"].sum()
        if cap > 0:
            st.loc[rows, "min_gen_frac"] = min(1.0, chp_mw * hf / cap)
    return st


def nb_window(zone, stack, nl, res, prices, T) -> dict:
    """Neighbour zone dict for one window: block SRMC, measured seasonal must-run (DE_LU), DSR tranches,
    reservoir budget from the window's actual generation."""
    st = apply_water_value(stack.assign(srmc_eur_mwh=srmc(stack, prices).to_numpy()))
    st = apply_measured_mustrun(st, zone, T)                     # DE_LU: MaStR-measured seasonal must-run
    w = nl.reindex(T).interpolate().ffill().bfill()
    st = pd.concat([st, dsr_tranches(zone, float(w["load_mw"].max()))], ignore_index=True)
    budget = float(res.reindex(T).fillna(0).sum()) if len(res) else 0.0
    caps = {"hydro_reservoir": budget} if budget > 0 and (st["tech"] == "hydro_reservoir").any() else {}
    return {"stack": st, "demand": w["load_mw"].to_numpy(), "res_pot": w["musttake_res_mw"].to_numpy(),
            "avail": None, "energy_caps": caps}
