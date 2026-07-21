"""FR unit-level economic stack: fleet + econ params → per-unit SRMC merit order for a month's prices.

Assigns each thermal unit an electrical efficiency dispersed within its class band (seeded, reproducible)
so the mid-merit curve has a realistic slope; nuclear/hydro get flat proxy costs. Flexibility parameters
(min-generation fraction, ramp fraction) feed the LP's relaxed-commitment constraints. Availability is
applied at solve time, not here.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import Config
from .costs import CO2_INTENSITY_TH, EFF_RANGE, FUEL_COMMODITY, VOM, fuel_eur_mwh_th, nuclear_srmc

# per-tech flexibility: (min_gen_fraction when committed/available, max ramp per hour as frac of capacity)
FLEX = {
    "nuclear": (0.25, 0.05),          # modulation floor + slow ramp (sets how often FR prices hit ~0)
    "gas": (0.0, 1.0), "ccgt": (0.0, 0.7), "ocgt": (0.0, 1.0), "coal": (0.0, 0.4),
    "oil": (0.0, 1.0), "biomass": (0.3, 0.3), "waste": (0.6, 0.2),
    "hydro_reservoir": (0.0, 1.0), "hydro_ror": (0.9, 0.2), "hydro_psp": (0.0, 1.0),
}
_THERMAL = {"gas", "coal", "lignite", "oil", "biomass"}


def build_fr_stack(config: Config, fleet: pd.DataFrame | None = None,
                   year: int | None = None) -> pd.DataFrame:
    """→ per-unit stack with static attributes (efficiency, min_gen, ramp). SRMC added per month by `srmc`.

    Avec `year`, le parc est celui de l'année (unités réellement en service) et l'écart avec la capacité
    installée RTE est comblé par un bloc agrégé par filière — voir `io.fr_fleet` pour les deux défauts
    corrigés. Sans `year`, comportement historique inchangé.
    """
    if fleet is None:
        from ..io.fr_fleet import load_fr_fleet
        fleet = load_fr_fleet(config, year)
    rng = np.random.default_rng(config.seed)
    rows = []
    for _, u in fleet.sort_values("unit_id").iterrows():
        tech = u["tech"]
        lo, hi = EFF_RANGE.get(tech, (np.nan, np.nan))
        eff = float(rng.uniform(lo, hi)) if np.isfinite(lo) else np.nan   # per-unit dispersion
        mn, ramp = FLEX.get(tech, (0.0, 1.0))
        rows.append({"unit_id": u["unit_id"], "name": u["name"], "tech": tech,
                     "capacity_mw": float(u["capacity_mw"]), "efficiency": eff,
                     "min_gen_frac": mn, "ramp_frac": ramp, "vom": VOM.get(tech, 2.5)})
    st = pd.DataFrame(rows)
    if year is not None:
        st = _topup_to_installed(config, st, year, rng)
    return st


def _topup_to_installed(config: Config, st: pd.DataFrame, year: int, rng) -> pd.DataFrame:
    """Comble l'écart entre la capacité installée RTE et la somme des unités déclarées, par filière.

    Le parc diffus (petites centrales, cogénération, hydraulique de vallée) n'est pas publié groupe par
    groupe et manquait donc entièrement : -6 562 MW de lac, -4 151 MW de gaz, -1 266 MW de biomasse en 2024.

    Le bloc de complément prend le **bas** de la bande de rendement : ce sont précisément les unités trop
    petites ou trop anciennes pour être déclarées individuellement, donc les moins performantes du parc.
    Leur attribuer le rendement médian les placerait à tort trop bas dans l'ordre de mérite.
    """
    from ..io.fr_fleet import MIN_TOPUP_MW, installed_by_tech

    inst = installed_by_tech(config, year)
    if not inst:
        return st
    have = st.groupby("tech")["capacity_mw"].sum().to_dict()
    extra = []
    for tech, cap_inst in inst.items():
        gap = float(cap_inst) - float(have.get(tech, 0.0))
        if gap < MIN_TOPUP_MW or tech not in FLEX and tech not in EFF_RANGE:
            continue
        lo, hi = EFF_RANGE.get(tech, (np.nan, np.nan))
        mn, ramp = FLEX.get(tech, (0.0, 1.0))
        extra.append({"unit_id": f"FR_{tech}_diffus", "name": f"{tech} diffus", "tech": tech,
                      "capacity_mw": gap, "efficiency": float(lo) if np.isfinite(lo) else np.nan,
                      "min_gen_frac": mn, "ramp_frac": ramp, "vom": VOM.get(tech, 2.5)})
    return pd.concat([st, pd.DataFrame(extra)], ignore_index=True) if extra else st


def srmc(stack: pd.DataFrame, prices_month: dict[str, float]) -> pd.Series:
    """SRMC (€/MWh_el) per unit/block for one month's commodity prices. Works for FR unit stacks and
    aggregated neighbour blocks (VOM falls back to the per-tech default when no `vom` column).

    Vectorised over the stack (was a per-unit ``itertuples`` loop — a per-window hotspot once the LP build
    was moved to highspy). Byte-identical to the scalar form: non-fuel techs (hydro/psp/biomass/flex/…) get
    their VOM, nuclear its flat proxy, and each fuel-thermal tech ``fuel/η + CO2/η·EUA + VOM`` with the
    tech's per-unit efficiency.
    """
    tech = stack["tech"].to_numpy()
    # vom: the column value where present (NaN preserved, as the scalar `getattr(r,"vom",None)` did), else default
    if "vom" in stack.columns:
        vom = stack["vom"].to_numpy(dtype=float)
    else:
        vom = np.array([VOM.get(t, 2.5) for t in tech], dtype=float)
    out = vom.copy()                                     # non-fuel techs (hydro/psp/biomass/flex/import) = VOM
    eff = pd.to_numeric(stack.get("efficiency"), errors="coerce").to_numpy(dtype=float) \
        if "efficiency" in stack.columns else np.full(len(stack), np.nan)
    for t, fuel_c in FUEL_COMMODITY.items():             # gas/coal/lignite/oil (biomass fuel_c is None → VOM)
        if fuel_c is None:
            continue
        m = tech == t
        if m.any():
            a = fuel_eur_mwh_th(fuel_c, prices_month) + CO2_INTENSITY_TH.get(t, 0.0) * prices_month["co2"]
            out[m] = a / eff[m] + vom[m]
    out[tech == "nuclear"] = nuclear_srmc()
    return pd.Series(out, index=stack.index, name="srmc_eur_mwh")
