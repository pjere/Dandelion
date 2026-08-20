"""Projection-mode dispatch — 2027-46 price trajectories (step vii finale).

The backtest clears historical years against ENTSO-E actuals. Projection clears **future** years: it takes
a reference historical year for the hourly *weather shape* (demand / RES / hydro profiles) and evolves the
*structure* forward — RES build-out, demand growth, coal phase-out, forward commodity prices, and the
**year-varying RES subsidy bid stack** (`scheme_shares(zone, year)` — vintages roll off support, new build
enters merchant/CfD, the §51 trigger tightens 6h→1h). So the trajectory shows RES growth pushing *more*
surplus while the scheme roll-off makes the resulting negatives *shallower and shorter* — the two effects
the static tab could never capture.

Structural evolution comes from TYNDP capacity/demand trajectories where the `dispatch_tyndp` tab provides
them (#76, incl. the `cap_flex_gw` adequacy fleet, #83), falling back to per-tech CAGRs
(`dispatch_projection` tab). Weather comes from the fixed reference-year shape by default, or — via the
`weather_shapes` hook (#77) — from a re-drawn weathergen realization (FR exact through steps iii/iv;
neighbours reduced-form, see `weather_shapes.py`). Stochastic neighbour availability (#80) derates firm
stacks per Monte-Carlo draw from REMIT-calibrated spreads (opt-in via `avail_rng`/`avail_years`).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..commodities.gas_rules import load_gas_rules
from ..commodities.model import CommodityModel, load_zone_basis, zone_prices
from ..commodities.resolve import PriceResolver
from ..config import Config
from ..io.entsoe_hist import load_generation_hist
from ..io.fr_history import load_fr_netload
from ..markup import apply_markup
from ..neighbours.blocks import build_neighbour_stack, constituents, neighbour_netload
from ..lp.highs_solver import solve_budget
from ..res_schemes import load_res_schemes, solve_with_triggers
from ..rules import rules_at
from ..hydro.water_value import season_of as wv_season_of
from ..scheme_evolution import scheme_shares, trigger_hours
from ..stacks.costs import flex_vom
from ..tyndp import (flex_capacity_mw, load_ntc_newbuild, load_tyndp, ntc_delta_mw, report_coverage,
                     tyndp_factors)
from .assemble import (_EXCLUDE_DISPATCH, MEASURED_MUSTRUN_ZONES, NTC, flow_derived_ntc,
                       hourly_ntc, modelled_zones, slice_ntc)
from .windows import fr_stack_base, fr_window, nb_window

# default structural CAGRs (editable — dispatch_projection tab). RES build-out dominates the negative-price
# story; demand creeps up with electrification; coal/lignite phase down.
_GROWTH = {"demand": 0.008, "res": 0.045, "coal": -0.08, "lignite": -0.08}

#: wall-clock ceiling for ONE weekly window, across every solve it triggers (§51 fixed point + the
#: seam→cold fallback). 300 s is ~30x a healthy window (measured 3-17 s) so it can only bind on the
#: degenerate class, and it caps a 52-week year at ~4 h even if every window were pathological.
_WINDOW_BUDGET_S = 300.0


def _load_growth(wb) -> dict:
    from powersim_core.scenario import load_sheet
    try:
        df = load_sheet(wb, "dispatch", "projection")
    except (ValueError, KeyError):
        return dict(_GROWTH)
    g = dict(_GROWTH)
    for r in df.itertuples():
        g[str(r.variable)] = float(r.annual_growth) / 100.0    # tab stores %/yr → fraction
    return g


_HYDRO_EFF, _HYDRO_CO2 = 0.50, 0.202      # the marginal thermal unit water competes against (CCGT-ish)


def _hydro_level_ratio(ref: dict, target_year: int) -> float:
    """Water-value level multiplier target/reference, from the exogenous commodity trajectory.

    Water is worth what it displaces, so its level tracks the marginal thermal SRMC — a scenario
    quantity, available for any future year and independent of observed prices and of the LP. Returns
    1.0 (no re-levelling) when the resolver cannot price either year."""
    res = ref.get("resolver")
    if res is None:
        return 1.0

    def _srmc(year: int) -> float:
        vals = []
        for m in (1, 4, 7, 10):                       # quarterly probes → an annual mean, cheap
            try:
                p = res.prices_at(pd.Timestamp(f"{year}-{m:02d}-15", tz="UTC"))
            except Exception:                          # noqa: BLE001 — missing year → skip the probe
                continue
            vals.append(p["gas"] / _HYDRO_EFF + (_HYDRO_CO2 / _HYDRO_EFF) * p["co2"])
        return float(np.mean(vals)) if vals else float("nan")

    a, b = _srmc(int(ref.get("ref_year", 2019))), _srmc(int(target_year))
    if not (np.isfinite(a) and np.isfinite(b)) or a <= 0:
        return 1.0
    return float(np.clip(b / a, 0.2, 5.0))            # bounded: a scenario shock must not invert the curve


def _hydro_expander(ref: dict, target_year: int):
    """→ f(stack, zone) applying the reference-year water-value tranches, re-levelled to `target_year`.

    Identity when no curve was calibrated for the zone (never substitute a default for missing data).
    The re-level shifts only the ARBITRAGED tranches by w̄·(ratio−1), w̄ being their capacity-weighted
    mean: the reserved-flow tranche (cheapest) and the scarcity tranche (dearest) are physical anchors,
    not prices, and `shift_hydro_bids` holds them fixed."""
    curves = ref.get("hydro_curves") or {}
    if not curves:
        return lambda st, z: st
    from ..hydro.synthesis import shift_hydro_bids
    from ..hydro.water_value import expand_stack as _wv_expand
    from ..stacks.revealed import BID_COL as _BID
    lvl = _hydro_level_ratio(ref, target_year)

    def apply(st, zone):
        st = _wv_expand(st, curves, zone)
        if not lvl or abs(lvl - 1.0) <= 1e-6 or _BID not in st.columns:
            return st
        m = (st["tech"] == "hydro_reservoir").to_numpy()
        if m.sum() <= 2:
            return st
        bids = st.loc[m, _BID].to_numpy(float)
        caps = st.loc[m, "capacity_mw"].to_numpy(float)
        mid = np.argsort(bids)[1:-1]                            # drop both anchors
        if mid.size == 0 or not np.isfinite(bids[mid]).all():
            return st
        wbar = float(np.average(bids[mid], weights=np.maximum(caps[mid], 1e-9)))
        return shift_hydro_bids(st, wbar * (lvl - 1.0), bid_col=_BID)

    return apply


def _scale_stack(stack: pd.DataFrame, k: int, g: dict, cap_factors: dict | None = None) -> pd.DataFrame:
    """Evolve a stack's firm capacity to the projection year.

    With TYNDP (`cap_factors` = {tech: target/ref multiplier}, #76) each tech is scaled to its TYNDP
    trajectory directly — the scenario already encodes retirements *and* new build, so no synthetic
    coal→gas replacement is needed. Without TYNDP, the CAGR fallback phases down coal/lignite but
    **replaces the retired firm MW 1:1 with new CCGT** so the reserve margin is preserved (otherwise the
    projection removes ~80 % of coal by 2040 with nothing behind it and manufactures false VoLL scarcity)."""
    st = stack.copy()
    if cap_factors:
        for tech, f in cap_factors.items():
            m = st["tech"] == tech
            if m.any():
                st.loc[m, "capacity_mw"] *= f
        return st
    retired = 0.0
    for tech in ("coal", "lignite"):
        m = st["tech"] == tech
        before = st.loc[m, "capacity_mw"].sum()
        st.loc[m, "capacity_mw"] *= (1 + g[tech]) ** k
        retired += before - st.loc[m, "capacity_mw"].sum()
    gas = st["tech"] == "gas"
    if retired > 10 and gas.any():                             # new CCGT replaces the retired firm capacity
        st.loc[gas, "capacity_mw"] *= 1 + retired / st.loc[gas, "capacity_mw"].sum()
    return st


#: years tried, in order, for the hydro water-value curve SHAPE when the reference year cannot resolve
#: one (see `_preload`). Recent years carry the wide price distributions that populate the middle price
#: classes; the reference year is always the last fallback so behaviour is unchanged where it resolves.
_HYDRO_SHAPE_YEARS = (2024, 2023, 2025)
#: years tried, in order, for the MEASURED must-run floor. A p10-of-observed floor is only a FORCED
#: minimum in a year with enough out-of-the-money hours to push the plant down to it — see the note at
#: the `nb_mustrun` build. Recent high-RES years reveal it; a year where the plant simply ran does not.
_MUSTRUN_REVEAL_YEARS = (2024, 2025)
#: zones whose RES registry is genuinely plant-level, so the year-evolved `scheme_shares` ladder is
#: meaningful. Everything else uses the static `dispatch_res_schemes` tab — see the comment at the
#: `schemes = {...}` assignment, and the matching policy in `run_backtest`.
_REGISTRY_EVOLVED = ("DE_LU", "FR")
#: one-shot latch so a 20-year cross-over prints the TYNDP coverage report once, not 20 times
_COVERAGE_REPORTED: list[int] = []


def _append_flex(stack: pd.DataFrame, zone: str, tyndp: dict, year: int) -> pd.DataFrame:
    """Append the zone's 2040-flexibility fleet (battery + DR + H2-peaker) as one dispatchable block from the
    TYNDP `cap_flex_gw` trajectory (#83). Priced at its VOM by `srmc`, it is the adequacy backstop
    that caps scarcity as firm thermal retires — no-op where TYNDP gives no flex for the zone.

    The row MUST carry `vom`. `srmc()` reads the stack's `vom` COLUMN when one exists and only falls back
    to the per-tech `VOM` table when it does not — and these stacks do have the column. So omitting `vom`
    here bypassed the flex bid (`VOM["flex"]`, since raised 180 → 300 by the workbook owner; take it from
    `costs.flex_vom()`, which honours the `DISPATCH_FLEX_VOM` sensitivity override) and left the row at
    NaN, since `srmc()` starts from `out = vom.copy()` and `flex` is a
    non-fuel tech that never gets a fuel term to overwrite it. Writing `srmc_eur_mwh` here would not
    work: the column does not exist at this point (the stack is `unit_id, name, tech, capacity_mw,
    efficiency, min_gen_frac, ramp_frac, vom`), so `row.get(c, ...) for c in stack.columns` drops it, and
    the per-window `srmc()` would overwrite it anyway.
    """
    mw = flex_capacity_mw(tyndp, zone, year) if tyndp else 0.0
    if mw < 50:
        return stack
    row = {"unit_id": f"{zone}_flex", "zone": zone, "tech": "flex", "capacity_mw": float(mw),
           "efficiency": np.nan, "min_gen_frac": 0.0, "vom": flex_vom()}
    return pd.concat([stack, pd.DataFrame([{c: row.get(c, np.nan) for c in stack.columns}])],
                     ignore_index=True)


def _window_ntc(ref, border, T, target_year: int):
    """(fwd, bwd) for one border, = the reference-year series + whatever was commissioned since.

    The reference series carries the hourly structure (outages, flow-based domain shrinkage); a new link
    adds its rating on top of that structure rather than rescaling it, which is why this is an ADDITION and
    not a multiplication. Clipped at zero so a backwards projection past a commissioning year cannot drive
    a border negative.

    ONE CONSEQUENCE TO KNOW. Addition means an hour where the reference year had the border at 0 MW comes
    out at the delta, not at 0 — a corridor that was shut in 2019 carries 2 GW in 2025 if two cables were
    laid since. That is right when the reference-year zero is an outage of the EXISTING link, because
    IFA2 and ElecLink are independent cables that do not care whether IFA is out; it is optimistic when the
    zero is a whole-corridor event (a collapsed flow-based domain), where the new cable would likely be
    constrained too. Multiplying instead would fix the second case and break the first — it would leave a
    closed border closed forever regardless of what was built — so the addition is deliberate, not an
    oversight. Measured on 2024, 13 of 16 published directions touch 0 MW at some point, so this is not a
    rare corner.
    """
    fwd, bwd = slice_ntc(ref["ntc"], border, T)
    d = ntc_delta_mw(ref.get("ntc_newbuild") or {}, border, target_year, int(ref["ref_year"]))
    if not d:
        return fwd, bwd
    return (np.maximum(np.asarray(fwd, dtype=float) + d, 0.0),
            np.maximum(np.asarray(bwd, dtype=float) + d, 0.0))


def project_year(config: Config, target_year: int, ref, n_weeks: int | None = None,
                 avail_rng=None, weather_shapes: dict | None = None,
                 return_prices: bool = False, draw: int = 0, sink: dict | None = None) -> pd.DataFrame:
    """Clear `target_year` from the preloaded reference-year shapes in `ref`; return per-zone price stats.

    `draw` selects the Monte-Carlo draw for the per-draw feeds (step-v FR nuclear availability, #160).

    `sink` (opt-in) is a caller-supplied dict the year's VOLUMES are written into — `"dispatch"` the
    per-(zone, tech) MW frame, `"flows"` the per-border net MW, `"smc"` the pre-markup duals. The stats
    frame this function returns is a price summary, and prices alone cannot give revenues or congestion
    rents: those need the volume each price was paid on. Filling `sink` needs `DISPATCH_CAPTURE_DISPATCH`
    set for the dispatch part (flows and SMC are already in the solver output either way); left None,
    nothing is retained and the memory profile is what it always was."""
    zones, neigh, wb, cm, basis, floors, g = (ref[key] for key in
                                              ("zones", "neigh", "wb", "cm", "basis", "floors", "growth"))
    k = target_year - ref["ref_year"]
    # #76: per-zone demand/RES/capacity multipliers from TYNDP where available, else the flat CAGR fallback.
    tyndp = ref.get("tyndp") or {}
    # Report the gaps EVERY run. The 2024 backcast measured what silence costs: CH and IT_NORTH have no
    # 2019 RES anchor, so their factors clamp to exactly 1.0 and RES is frozen at reference-year level;
    # NL has no RES/demand/flex rows at all and falls back to a flat CAGR. All three lost their entire
    # negative-price tail (CH 292→0, NL 458→0 hours) and ran +16 to +20 €/MWh high, and nothing in the
    # output distinguished that from a deliberate flat scenario. Printed once per projected year, not
    # raised: a partially-filled tab is a legitimate work-in-progress state, an unnoticed one is not.
    if tyndp and not _COVERAGE_REPORTED:
        # de-duplicate: `zones` already contains the neighbours, so zones+neigh lists most of them twice
        gaps = report_coverage(tyndp, list(dict.fromkeys(list(zones) + list(neigh))), ref["ref_year"])
        if gaps:
            print(f"  [projection] TYNDP coverage gaps vs ref {ref['ref_year']} "
                  f"(clamped = frozen at reference level, missing = CAGR fallback):", flush=True)
            for ln in gaps:
                print(ln, flush=True)
        _COVERAGE_REPORTED.append(1)

    def zfac(zone):
        tf = tyndp_factors(tyndp, zone, target_year, ref["ref_year"]) if tyndp else None
        dem = tf["demand"] if tf and tf.get("demand") else (1 + g["demand"]) ** k
        res = tf["res"] if tf and tf.get("res") else (1 + g["res"]) ** k
        cap = tf["cap"] if tf and tf.get("cap") else None
        return dem, res, cap

    # #77: weather-coherent net-load shapes. `weather_shapes` = {zone: hourly df} where the demand/RES are
    # already at the target year's structure AND a *re-drawn* weathergen weather shape (from the demand/RES
    # models), so they REPLACE both the fixed 2019 shape and the demand/RES growth factors. Absent zones fall
    # back to the reference-year shape scaled by TYNDP/CAGR (the current behaviour). This is the hook the
    # full weather-ensemble projection plugs into; today FR is wireable (its models exist), neighbours need
    # their own demand/RES models (the remaining build).
    wshapes = weather_shapes or {}

    def _align(df):
        """Re-index a target-year weather shape onto the reference-year hourly calendar (by position) so the
        existing weekly-window machinery slices it — the shape supplies the VALUES, the ref calendar the
        windowing. Leap-day excess is truncated."""
        s = df.set_index("timestamp_utc") if "timestamp_utc" in df.columns else df.copy()
        ridx = pd.date_range(f"{ref['ref_year']}-01-01", f"{ref['ref_year'] + 1}-01-01", freq="h", tz="UTC")[:-1]
        n = min(len(s), len(ridx))
        s = s.iloc[:n].copy()
        s.index = ridx[:n]
        return s

    fr_dem, fr_res, fr_cap = zfac("FR")
    if "FR" in wshapes:
        fr = _align(wshapes["FR"])
        # carry the reference year's nuclear/reservoir generation shapes (the availability proxy + hydro
        # budget the FR window needs) — these are maintenance-scheduled, not weather-driven, so the ref-year
        # seasonal pattern is a fair proxy; #80 handles their stochastic year-to-year variation separately.
        for col in ("gen_nuclear_mw", "gen_hydro_reservoir_mw"):
            if col in ref["fr"].columns:
                fr[col] = ref["fr"][col].reindex(fr.index).ffill().bfill().to_numpy()
    else:
        fr = ref["fr"].copy()
        fr["demand_mw"] = fr["demand_mw"] * fr_dem
        fr["musttake_res_mw"] = fr["musttake_res_mw"] * fr_res
    fr_stack = _append_flex(_scale_stack(ref["fr_stack"], k, g, cap_factors=fr_cap), "FR", tyndp, target_year)
    # water value — the projection was MISSING this: `run_backtest` expands the single `hydro_reservoir`
    # block into calibrated water-value tranches, `_preload` never did, so every projected year dispatched
    # reservoirs at ~1 €/MWh VOM under a hard reference-year energy budget (an all-or-nothing budget dual
    # instead of a graded opportunity cost). Expanded on the REFERENCE year's curves — the tranche SHAPE
    # (reserved flow / arbitraged water / scarcity) is hydrological and transfers — then RE-LEVELLED to the
    # target year, because the level is a price and does not (a 2019 curve belongs to a €39/MWh world).
    # MUST run before `build_flex_spec`: the flex spec indexes stack ROWS, and expanding changes them.
    _wv = _hydro_expander(ref, target_year)
    fr_stack = _wv(fr_stack, "FR")
    # FLEX module (opt-in): keep the per-reactor nuclear rows and attach the C1/C2/C3/C5 rigidity spec.
    # A future year has no observed price, so `load_curve` returns None — and the bids then fell back to
    # the fuel cost for the WHOLE fleet: 100 % of FR nuclear at 7 €/MWh, the degeneracy the revealed curve
    # exists to prevent (in backtest it caught ~88 % of the fleet and pinned the FR price at 7 for a third
    # of the year; measured here as a projection median stuck at exactly 7.0). `default_curve()` supplies
    # the measured 2019-24 mean SHAPE instead — utilisation shares transfer across years where price
    # levels do not — and the free-mass re-map then prices the offered band along it.
    from ..flexibility import enabled as _flex_enabled
    flex_spec = None
    flex_costs = None                                      # kept for the neighbour specs built below
    nb_flex: dict = {}                                     # neighbour-zone flex specs (static per year)
    fired_floor = 0.0                                      # flag-off: historic §51 fired-tranche floor
    if _flex_enabled(config):
        from ..flexibility import fr_nuclear, trajectories
        from ..stacks import nuclear_curve as nuc
        nuc_installed = float(fr_stack.loc[fr_stack["tech"] == "nuclear", "capacity_mw"].sum())
        costs = flex_costs = trajectories.load_flex_costs(wb, target_year)
        reserves = trajectories.load_reserves(wb, target_year)
        fr_stack, flex_spec = fr_nuclear.build_flex_spec(
            fr_stack, nuc.load_curve(config, target_year, nuc_installed) or nuc.default_curve(),
            c_mod=costs["c_mod"], c_start_by_class=costs,
            r_up_req=reserves["r_up_req"], r_down_req=reserves["r_down_req"],
            p_minstab=trajectories.minstab_mw(wb, "FR", target_year),
            include_fossil=True, fossil_c_start=costs)
        fired_floor = 0.0                              # fired §51 tranches bid the German-law 0.0 (aligned
        #                                                with the backtest revert: the −0.01 variant
        #                                                mass-printed phantom negatives, A/B DE 545 vs 70)
        # C4 maneuverability in projection comes from the planned-outage scheduler (F1) — a separate hook not
        # yet wired here; projected reactors run `full` until it lands, same documented degrade as the backtest.
    nb_fac = {z: zfac(z) for z in neigh}
    nb_stack = {z: _append_flex(_scale_stack(s, k, g, cap_factors=nb_fac[z][2]), z, tyndp, target_year)
                for z, s in ref["nb_stack"].items()}
    nb_stack = {z: _wv(s, z) for z, s in nb_stack.items()}      # same water-value treatment (see above)
    # neighbour-zone rigidity — the LAST instance of the one-price defect. `project_year` used to pass
    # `flex={"FR": …}` only, so BE/CH/ES nuclear stayed a single free block bidding a flat SRMC in every
    # projected year: ~10 GW of the exact degeneracy the FR fix removed, sitting across the border where
    # a backtest-based gate cannot see it. The specs consume only workbook trajectories + the measured
    # per-zone anchors (`neighbour_nuclear._ANCHORS`) — no observed price — so they are projection-legal.
    # Built AFTER the stacks are final: `split_nuclear_block` adds pseudo-unit ROWS and the spec indexes
    # rows (the same ordering constraint the hydro expansion above has).
    if flex_costs is not None:
        from ..flexibility import neighbour_nuclear as nnuc
        for z in list(nb_stack):
            st_z = nnuc.split_nuclear_block(nb_stack[z], z)
            spec_z = nnuc.build_neighbour_flex_spec(st_z, z, flex_costs)
            if spec_z is not None:
                nb_stack[z] = st_z
                nb_flex[z] = spec_z
    # storage in PROJECTION (flex-gated; the machinery's real home per the backtest verdict — commit
    # 0f84fb6): measured PSP envelopes + the BESS build-out trajectory replace the battery share of #83's
    # crude `cap_flex_gw` block, which is SHRUNK by the BESS power added (no double count). No observed-
    # count gate exists here; storage is essential to 2030+ price formation (it is what caps the spreads).
    storage_proj = None
    if flex_spec is not None:
        from ..flexibility.storage import bess_power_mw, storage_spec
        storage_proj = storage_spec({}, target_year, tyndp=tyndp) or None
        for z, st_z in [("FR", fr_stack)] + list(nb_stack.items()):
            b = bess_power_mw(z, target_year, tyndp=tyndp)
            m = st_z["unit_id"] == f"{z}_flex"
            if b > 0 and m.any():
                st_z.loc[m, "capacity_mw"] = (st_z.loc[m, "capacity_mw"] - b).clip(lower=0.0)
    # #80: stochastic neighbour availability — per Monte-Carlo draw, derate neighbour firm capacity by a
    # mean-preserving REMIT-calibrated multiplier (≈1.0, no double-count with the p99 proxy). Central path
    # (avail_rng=None) leaves the stacks unchanged.
    avail_stats = ref.get("avail_stats") or {}
    if avail_rng is not None and avail_stats:
        from ..neighbour_availability import apply_multipliers, availability_multipliers
        nb_stack = {z: apply_multipliers(st, availability_multipliers(avail_stats.get(z, {}), avail_rng))
                    for z, st in nb_stack.items()}
    nb_nl = {}
    for z in neigh:
        if z in wshapes:                            # #77: weather-coherent neighbour shape (when a model exists)
            nb_nl[z] = _align(wshapes[z])
            continue
        w = ref["nb_nl"][z].copy()
        w["load_mw"] = w["load_mw"] * nb_fac[z][0]
        w["musttake_res_mw"] = w["musttake_res_mw"] * nb_fac[z][1]
        nb_nl[z] = w

    # year-varying RES subsidy tranches (roll-off + new build + §51 trigger schedule). The registry read
    # is hoisted into `_preload` (`res_registry`) so it is not re-read from the lake once per year.
    #
    # ONLY the zones with a genuine plant-level registry get the evolved ladder. This mirrors the policy
    # `run_backtest` already applies and documents: DE_LU's registry is plant-level MaStR and its §51
    # trigger semantics are genuinely German, whereas the other zones' registries are a degenerate
    # cohort tier — 4 rows for BE/ES/IT_NORTH, none at all for NL — and `scheme_shares` would bolt a
    # German trigger onto paid-regardless certificate schemes that have none. The projection was
    # applying it EVERYWHERE, and the `or static` fallback only catches the empty case, not the
    # degenerate one. Measured on the 2024 backcast, before this fix:
    #
    #   BE  registry yields ONE `green_certificate` tranche, and that name does not exist in the
    #       workbook tab (which has gc_residential/gc_offshore/merchant), so `floors.get()` missed and
    #       the whole Belgian ladder collapsed to a single rung at 0.0 -> ZERO negative hours projected
    #       against 404 observed, mean +11.6 EUR/MWh.
    #   CH  registry yields `kev` only, dropping the merchant rung -> floors [-50] instead of [-50, 0].
    #   NL  registry is EMPTY, so the `or static` fallback already rescued it (floors -500/-20/-2).
    #
    # FR keeps the evolved ladder: 132k plant-level rows, and `apply_oa_ladder` below depends on the
    # vintage split it produces.
    res_registry = ref.get("res_registry") or {}
    # Re-read the static tab AT THE TARGET YEAR, not the reference year: the ladder is year-indexed where
    # the tab dates its rows (ES has no negative regime before 2024, -0.10 median in 2024, -1.00 in 2025),
    # and `_preload` necessarily loaded it once for a whole horizon. Falls back to the preloaded block if
    # the workbook cannot be re-read.
    try:
        _static_y = load_res_schemes(wb, target_year) or ref["static"]
    except (ValueError, KeyError):
        _static_y = ref["static"]
    schemes = {z: ((scheme_shares(z, target_year, floors.get(z, {}), reg=res_registry.get(z))
                    if z in _REGISTRY_EVOLVED else None) or _static_y.get(z, [])) for z in zones}
    if flex_spec is not None and "FR" in schemes:          # §6 (F4): FLEX owns the FR bid ladder. The OA
        from ..flexibility.trajectories import apply_oa_ladder, load_oa_ladder  # *volume* still decays by
        schemes["FR"] = apply_oa_ladder(schemes["FR"], load_oa_ladder(wb, target_year))  # vintage via scheme_shares

    # #160: FR nuclear availability from the step-v availability_model lake for this draw, as
    # {reference-year date → nuclear outage MW at THIS year's nuclear capacity}. Passed to `fr_window`, which
    # then uses the true projected fleet condition (maintenance + forced outages) instead of the reference-
    # year rolling-max-of-output proxy. None (lake absent) → the proxy is retained. Reservoir energy budget
    # is overridden per window inside the loop.
    fr_avail = ref.get("fr_avail")
    nuc_unavail = None
    if fr_avail is not None:
        from .fr_availability import nuclear_unavail_daily
        _nuc_cap = float(fr_stack.loc[fr_stack["tech"] == "nuclear", "capacity_mw"].sum())
        nuc_unavail = nuclear_unavail_daily(fr_avail, target_year, ref["ref_year"], draw, _nuc_cap)

    price_chunks = []
    disp_chunks, flow_chunks = [], []   # volumes, only when the caller passed a sink
    _dropped: list[str] = []            # windows the solve budget could not clear (reported below)
    prev_flex_state = None; prev_w1 = None                     # F5: FR tail state across adjacent window seams
    for w0, w1 in zip(ref["weeks"][:-1], ref["weeks"][1:]):
        T = fr.loc[(fr.index >= w0) & (fr.index < w1)].index
        if len(T) < 24:
            prev_flex_state = None
            continue
        w0_t = w0 + pd.DateOffset(years=k)                     # commodity + market-rule year = the target
        prices = ref["resolver"].prices_at(w0_t)
        # SEASONAL water value. The annual curve is a single object for the whole year, so summer water
        # is priced at the annual average — measured on ES 2024, 28.4 EUR/MWh when the summer arbitraged
        # water is actually worth 75.5. The projection therefore dumps Iberian hydro all summer, which is
        # the leading candidate for its ES negative overshoot (2233 projected vs 247 observed, mean 47
        # vs 63). Applied here per window because the stacks are expanded once per YEAR; the shift hits
        # only the arbitraged tranches, leaving the reserved-flow and scarcity anchors alone.
        _season = wv_season_of(w0_t.month)
        _wvd = {z: d.get(_season) for z, d in (ref.get("wv_seasonal") or {}).items()}
        zd = {"FR": fr_window(fr, fr_stack,
                              zone_prices(prices, "FR", basis, w0_t, ref.get("gas_rules")), T,
                              nuc_unavail_daily=nuc_unavail, wv_delta=_wvd.get("FR"))}
        if fr_avail is not None:                                # #160: step-v reservoir energy budget for the week
            from .fr_availability import reservoir_budget_mwh
            _rb = reservoir_budget_mwh(fr_avail, target_year, w0_t)
            if _rb is not None and _rb > 0 and zd["FR"].get("energy_caps"):
                zd["FR"]["energy_caps"]["hydro_reservoir"] = _rb
        for z in neigh:
            zd[z] = nb_window(z, nb_stack[z], nb_nl[z], ref["nb_res"][z],
                              zone_prices(prices, z, basis, w0_t, ref.get("gas_rules")), T,
                              wv_delta=_wvd.get(z),
                              mustrun_floors=(ref.get("nb_mustrun") or {}).get(z))
        borders = [b for b in NTC if b[0] in zd and b[1] in zd]
        res_bid, price_floor = rules_at(wb, w0_t, list(zd))
        cold = seam = None
        if flex_spec is not None:
            cold = dict(flex_spec)
            seam = {**cold, **prev_flex_state} if (prev_flex_state is not None and prev_w1 == w0) else cold

        # seam-linked first, cold fallback if it over-constrains the window (see run_backtest for the rationale)
        # Bounded by a per-WINDOW budget: a per-solve limit does not bound the window, because the §51 fixed
        # point re-solves and the seam→cold fallback both multiply it — and crossover overruns its own limit.
        # Measured before this guard: one high-RES 2024 window burned 85 CPU-minutes. A window that cannot be
        # solved inside the budget is dropped and counted, exactly as run_backtest drops its pathological
        # windows, instead of stalling the whole horizon.
        out = None
        with solve_budget(_WINDOW_BUDGET_S):
            for sp in ([seam, cold] if seam is not cold else [seam]):
                try:
                    out = solve_with_triggers(T, zd, borders,
                                              {b: _window_ntc(ref, b, T, target_year) for b in borders},
                                              schemes,
                                              res_bid=res_bid, price_floor=price_floor,
                                              flex=({**{z: s for z, s in nb_flex.items() if z in zd},
                                                     "FR": sp} if sp is not None else None),
                                              fired_floor=fired_floor, storage=storage_proj)
                    break
                except RuntimeError:
                    out = None
        if out is None:
            _dropped.append(str(w0.date()))
        if out is None:
            prev_flex_state = None; continue
        price_chunks.append(out["prices"])
        if sink is not None:
            # windows are half-open [w0, w1) on a shared calendar, so these concatenate without overlap
            if out.get("dispatch") is not None:
                disp_chunks.append(out["dispatch"])
            if out.get("flows") is not None and len(out["flows"]):
                flow_chunks.append(out["flows"])
        if flex_spec is not None and out.get("flex", {}).get("FR") is not None:
            from ..flexibility.fr_nuclear import tail_state
            prev_flex_state = tail_state(out["flex"]["FR"]); prev_w1 = w1
        if n_weeks and len(price_chunks) >= n_weeks:
            break
    if not price_chunks:
        raise RuntimeError(f"projection {target_year}: every weekly LP window failed to solve")
    if _dropped:
        print(f"  [projection] {len(_dropped)} window(s) dropped on the {_WINDOW_BUDGET_S:.0f}s budget: {_dropped}", flush=True)
    smc = pd.concat(price_chunks).sort_index()
    # step-vii price layer: lift SMC → spot with the fitted markup (skipped if no model on disk). Drivers
    # are the *projected* demand/RES against firm capacity — the same structural signals the wedge was fit
    # on — so the markup extrapolates on structure, not on a calendar year.
    markup = ref.get("markup")
    rows, spot = [], {}
    for z in zones:
        p_smc = smc[z].dropna()
        if markup is not None:
            drv = _zone_drivers_proj(z, fr, nb_nl, fr_stack, nb_stack, p_smc.index)
            p = apply_markup(markup, z, p_smc, drv)
        else:
            p = p_smc
        spot[z] = p
        rows.append({"year": target_year, "zone": z, "mean": round(p.mean(), 1), "smc_mean": round(p_smc.mean(), 1),
                     "neg_hours": int((p < 0).sum()),
                     "neg_mean": round(p[p < 0].mean(), 1) if (p < 0).any() else np.nan,
                     "trigger_h": trigger_hours(target_year)})
    stats = pd.DataFrame(rows)
    if sink is not None:
        sink["smc"] = smc
        sink["spot"] = pd.DataFrame(spot).sort_index()
        sink["dispatch"] = (pd.concat(disp_chunks).sort_index() if disp_chunks else None)
        sink["flows"] = (pd.concat(flow_chunks, ignore_index=True) if flow_chunks else None)
        sink["dropped"] = list(_dropped)
    return (stats, pd.DataFrame(spot).sort_index()) if return_prices else stats


def _zone_drivers_proj(zone, fr, nb_nl, fr_stack, nb_stack, idx) -> pd.DataFrame:
    """Projectable markup drivers [timestamp_utc, demand, musttake_res, firm_cap] for `zone` over `idx`, from the
    already-projected net loads and scaled firm stacks (mirrors ``markup.zone_drivers`` at fit time)."""
    from ..markup import _FIRM
    if zone == "FR":
        d = fr.reindex(idx)
        firm = float(fr_stack.loc[fr_stack["tech"].isin(_FIRM), "capacity_mw"].sum())
        return pd.DataFrame({"timestamp_utc": idx, "demand": d["demand_mw"].to_numpy(),
                             "musttake_res": d["musttake_res_mw"].to_numpy(), "firm_cap": firm})
    w = nb_nl[zone].reindex(idx)
    st = nb_stack[zone]
    firm = float(st.loc[st["tech"].isin(_FIRM), "capacity_mw"].sum())
    return pd.DataFrame({"timestamp_utc": idx, "demand": w["load_mw"].to_numpy(),
                         "musttake_res": w["musttake_res_mw"].to_numpy(), "firm_cap": firm})


def _preload(config: Config, ref_year: int, avail_years: list[int] | None = None) -> dict:
    zones = modelled_zones(config)
    neigh = [z for z in zones if z != "FR"]
    wb = config.resolve(config.section("assumptions")["workbook"])
    cm = CommodityModel.from_workbook(wb)
    fr = load_fr_netload(config, f"{ref_year}-01-01", f"{ref_year + 1}-01-01").set_index("timestamp_utc")
    nb_stack = {}
    for z in list(neigh):                          # a cluster with no ref-year data → drop it (parity with
        try:                                       # run_backtest); its stack build raises on the empty frame
            nb_stack[z] = build_neighbour_stack(config, z, ref_year)
        except (KeyError, ValueError):
            neigh.remove(z); zones.remove(z)
    nb_stack = {z: s[~s["tech"].isin(_EXCLUDE_DISPATCH)].reset_index(drop=True) for z, s in nb_stack.items()}
    nb_nl = {z: neighbour_netload(config, z, ref_year).set_index("timestamp_utc") for z in neigh}
    # NL behind-the-meter PV — the projection was MISSING this, exactly as it was missing the water value
    # below. `run_backtest` reconstructs it (backtest.py, `btm_solar`) because ~98 % of the Dutch solar
    # fleet is invisible on BOTH sides of the ENTSO-E balance: not in generation (behind the meter) and
    # not netted from the load series. Without it the real Dutch surplus does not exist in the inputs.
    #
    # Measured consequence on the 2024 backcast: NL projected 43 negative hours against 458 observed and
    # a p5 of +53 EUR/MWh against ~0 observed, even AFTER the TYNDP capacity anchors were fixed — because
    # the `res` factor was scaling a ~0.2 GW metered sliver instead of a ~29 GW fleet. And since BE's
    # negative prices are largely imported from the Dutch floor (FLEX_CALIBRATION_2024 §"Mechanisms now
    # demonstrably live"), the missing Dutch surplus propagated across the border.
    #
    # Reconstructed on the REFERENCE year, at that year's installed capacity, so `project_year`'s `res`
    # factor then scales the FULL fleet rather than the metered remnant.
    #
    # SUPERSEDED WHEN `io.unclassified_gen` IS ON — see the long note at the matching block in
    # `backtest.py`. The fleet is not invisible in generation; TenneT files it under `Other`, which no
    # dispatch class claimed. `neighbour_netload` now returns it already folded in, so reconstructing it
    # here as well would double-count (50.6 TWh at a 35.3 GW peak against a real ~21 TWh). The metered
    # series also carries the right peak — 13.04 GW vs this estimator's 22.28 — which is what the
    # projection's `res` factor then scales.
    from ..io.unclassified_gen import enabled as _unclassified_on, solar_enabled as _uc_solar
    if "NL" in neigh and not (_unclassified_on() and _uc_solar()):
        try:
            from ..flexibility.res_potential import btm_solar
            from ..io.entsoe_hist import load_installed_capacity
            _inst = float((load_installed_capacity(config, "NL", ref_year) or {}).get("solar", 0.0))
            _gen = load_generation_hist(config, ref_year, zones=constituents("NL"))
            _btm = btm_solar(_gen, _inst)
            if not _btm.empty and float(_btm.sum()) > 0:
                w = nb_nl["NL"]
                add = _btm.reindex(w.index).fillna(0.0)
                w["musttake_res_mw"] = w["musttake_res_mw"] + add
                w["netload_mw"] = w["load_mw"] - w["musttake_res_mw"]
                print(f"  [projection] NL BTM PV reconstructed on {ref_year}: "
                      f"+{float(add.mean()):.0f} MW mean ({_inst / 1e3:.1f} GW installed)", flush=True)
        except Exception as e:                                             # noqa: BLE001
            print(f"  [projection] NL BTM reconstruction unavailable ({type(e).__name__}) — "
                  f"NL surplus will be understated", flush=True)
    # water value — the projection was MISSING this entirely: `run_backtest` expands the single
    # `hydro_reservoir` block into the calibrated water-value tranches (backtest.py, `expand_stack`), but
    # `_preload` never did, so every projected year dispatched reservoirs at ~1 €/MWh VOM under a hard
    # ref-year energy budget — an all-or-nothing budget dual instead of a graded opportunity cost, i.e.
    # hydro dumping into cheap hours and no water-value support in the mid-band. Expanded here on the
    # REFERENCE year's curves (the same object the backtest calibrates); `project_year` then re-levels
    # them to the target year, since a 2019 water value belongs to a €39/MWh world.
    hydro_curves = {}
    wv_seasonal: dict = {}
    try:
        from ..hydro.water_value import load_curves, seasonal_level_deltas
        _hz = tuple(["FR"] + list(neigh))
        hydro_curves = load_curves(config, ref_year, _hz)
        # CURVE SHAPE IS NOT A REFERENCE-YEAR PROPERTY. `empirical_shares` needs MIN_HOURS_PER_BIN of
        # observations per price class; a reference year whose prices sat in a narrow band cannot
        # populate the middle classes and the curve collapses to its two anchors. Measured on ES:
        #
        #   2019  [(0.244, -15), (0.756, 200)]                   <- 2 tranches, NO arbitraged band
        #   2024  [(0.098, -15), (0.065, 0), (0.011, 10), ...]   <- 5 tranches, well formed
        #
        # A 24.4 % must-flow tranche bidding -15 EUR/MWh year-round is ~5 GW of Spanish reservoir dumped
        # into every cheap hour of every projected year: 2253 negative hours against 247 observed, mean
        # 47 vs 63. So the SHAPE is taken from the most recent year that actually resolves a curve, and
        # the LEVEL continues to come from `_hydro_level_ratio` — the same shape/level split the seasonal
        # deltas use. Per zone, because zones degenerate in different years.
        _shape_yr = {}
        for _y in (_HYDRO_SHAPE_YEARS + (ref_year,)):
            _c = load_curves(config, _y, _hz)
            for _z in _hz:
                if _z not in _shape_yr and len(getattr(_c.get(_z), "tranches", ())) > 2:
                    hydro_curves[_z] = _c[_z]; _shape_yr[_z] = _y
        _moved = {z: y for z, y in _shape_yr.items() if y != ref_year}
        if _moved:
            print(f"  [projection] hydro curve SHAPE taken from a resolving year (ref {ref_year} "
                  f"degenerate): {_moved}", flush=True)
        # per-season water-value shift, on the same year each zone's shape came from
        for _z, _y in _shape_yr.items():
            _d = seasonal_level_deltas(config, _y, (_z,)).get(_z)
            if _d:
                wv_seasonal[_z] = _d
        if wv_seasonal:
            print("  [projection] seasonal water value (EUR/MWh vs the annual curve): "
                  + ", ".join(f"{z} {d.get('summer', 0):+.0f} summer / {d.get('rest', 0):+.0f} rest"
                              for z, d in sorted(wv_seasonal.items())), flush=True)
    except (FileNotFoundError, KeyError, ValueError):
        hydro_curves = {}
    for z in list(neigh):                          # empty net-load → degenerate LP time coord → drop it too
        if nb_nl[z].empty:
            neigh.remove(z); zones.remove(z); nb_stack.pop(z, None); nb_nl.pop(z, None)
    nb_res = {}
    for z in neigh:
        gg = load_generation_hist(config, ref_year, zones=constituents(z))
        r = gg[gg["tech"] == "hydro_reservoir"]
        nb_res[z] = r.groupby("timestamp_utc")["gen_mw"].sum() if not r.empty else pd.Series(dtype=float)
    # MEASURED must-run floors (p10 of observed generation per tech-month). `run_backtest` has applied
    # these since the DE fix; the projection never did, so the two arms disagreed on forced supply.
    #
    # NOT calibrated on the reference year. p10-of-observed only reveals a FORCED floor in a year where
    # the plant is out of the money often enough to be pushed down to it; in a year where it is simply
    # economic, the same statistic measures profitable running. Measured on ES gas, p10 by month:
    #
    #     2019   4569 … 9532 MW      (gas in the money all year; Spain had ZERO negative hours)
    #     2024   1779 … 3450 MW      (RES surplus routinely pushes it to its real minimum)
    #
    # Taking 2019 would force MORE phantom gas into a 2024 projection than the flat 0.15 min_gen_frac
    # (4033 MW) it is meant to correct — the opposite of the fix. Same shape/level split as the hydro
    # curve above: the STRUCTURAL floor comes from a year that reveals it, the level from the scenario.
    nb_mustrun: dict = {}
    try:
        from ..neighbours.blocks import measured_chp_mw, observed_mustrun_floors
        _mz = [z for z in neigh if measured_chp_mw(z) or z in MEASURED_MUSTRUN_ZONES]
        for z in _mz:
            for _y in (*_MUSTRUN_REVEAL_YEARS, ref_year):
                f = observed_mustrun_floors(config, z, _y)
                if f:
                    nb_mustrun[z] = f
                    if _y != ref_year:
                        print(f"  [projection] {z} must-run floor from {_y} (ref {ref_year} does not "
                              f"reveal it): gas p10 "
                              f"{min(v.get('gas', 0) for v in f.values()):.0f}-"
                              f"{max(v.get('gas', 0) for v in f.values()):.0f} MW", flush=True)
                    break
    except (KeyError, ValueError):                       # no generation history → no floor, as before
        nb_mustrun = {}
    static = load_res_schemes(wb)          # ref-year block; project_year re-reads per target year
    from ..markup import load_model
    try:                                           # step-vii wedge; None → trajectories are raw SMC
        markup = load_model(config)
    except (FileNotFoundError, OSError):
        markup = None
    avail_stats = {}
    if avail_years:                                # #80: REMIT neighbour availability spread (opt-in; slow)
        from ..neighbour_availability import load_zone_stats
        avail_stats = load_zone_stats(neigh, avail_years)
    # #160: FR nuclear + reservoir availability from the step-v availability_model lake (default on) — replaces
    # the reference-year rolling-max nuclear proxy and the ref-year reservoir energy with the PROJECTED fleet
    # condition. None when the lake is absent → the reference-year proxy is retained (graceful).
    fr_avail = None
    if config.section("projection").get("availability_from_step_v", True):
        from .fr_availability import load_fr_availability
        fr_avail = load_fr_availability(config)
        if fr_avail is not None:
            print(f"  [projection] FR availability from step-v lake ({fr_avail['nuc_draws']} nuclear draw(s), "
                  f"reservoir budget {'on' if fr_avail['reservoir'] else 'off'})", flush=True)
    from powersim_core import registry  # read each zone's registry ONCE (year-independent) and keep only

    from ..io.fr_fleet import latest_fleet_year
    from ..scheme_evolution import RES_TECHS  # the RES rows scheme_shares needs — the full registry is
    res_registry = {}                              # ~170k rows/zone; the RES slice is tiny (matters for the
    for z in zones:                                # parallel MC, which holds `ref` once per worker).
        try:
            rz = registry.read(zone=z)
            res_registry[z] = rz[rz["tech"].isin(RES_TECHS) & rz["scheme"].notna()].copy()
        except (FileNotFoundError, KeyError, ValueError):
            res_registry[z] = None
    return {"zones": zones, "neigh": neigh, "wb": wb, "ref_year": ref_year, "markup": markup,
            "avail_stats": avail_stats, "fr_avail": fr_avail, "tyndp": load_tyndp(wb), "res_registry": res_registry,
            "cm": cm, "basis": load_zone_basis(wb), "resolver": PriceResolver(cm),
            "gas_rules": load_gas_rules(wb),
            "floors": {z: {t["scheme"]: t["floor"] for t in static.get(z, [])} for z in zones},
            "static": static, "growth": _load_growth(wb),
            "hydro_curves": hydro_curves, "wv_seasonal": wv_seasonal, "nb_mustrun": nb_mustrun,
            "fr": fr, "fr_stack": fr_stack_base(config, latest_fleet_year(config)), "nb_stack": nb_stack,
            "nb_nl": nb_nl, "nb_res": nb_res, # hourly published NTC on the REFERENCE year — the window index T is reference-year
            # timestamps, so it slices directly (see assemble.hourly_ntc)
            "ntc": hourly_ntc(config, ref_year, default=flow_derived_ntc(config, ref_year)),
            # interconnector commissioning steps, applied as a delta on the reference-year series above
            # (see `tyndp.ntc_delta_mw` for why a step delta and not an interpolated ratio)
            "ntc_newbuild": load_ntc_newbuild(wb),
            "weeks": pd.date_range(f"{ref_year}-01-01", f"{ref_year + 1}-01-01", freq="7D", tz="UTC")}


def project_trajectory(config: Config, years: list[int], ref_year: int = 2019,
                       n_weeks: int | None = None, weather_coherent: bool | None = None) -> pd.DataFrame:
    """Price trajectory across `years` from a single reference-year preload.

    By DEFAULT the FR demand/RES and neighbour net-loads come from the weather-coherent engines (#77,
    `weather_shapes.default_weather_provider`) on a re-drawn weathergen realization — NOT the fixed 2019
    weather. `ref_year` still supplies the hourly *calendar* the weekly windows slice, the firm-stack base,
    the NTC structure and the hydro/must-run curves; only the demand/RES *shapes* are re-drawn. Set
    `weather_coherent=False` (or config `projection.weather_coherent: false`) to fall back to the reshaped
    reference-year weather. If the engines or the weathergen cube are unavailable the run degrades to that
    same fallback with a warning, so environments without the FR models still work."""
    ref = _preload(config, ref_year)
    wc = (config.section("projection").get("weather_coherent", True)
          if weather_coherent is None else weather_coherent)
    provider = None
    if wc:
        from ..weather_shapes import default_weather_provider
        provider = default_weather_provider
    frames = []
    for y in years:
        ws = None
        if provider is not None:
            try:
                ws = provider(config, y, draw=0, ref_year=ref_year)
            except Exception as e:                                             # noqa: BLE001
                print(f"  [projection] weather-coherent engines unavailable "
                      f"({type(e).__name__}: {e}); falling back to reshaped reference-year {ref_year} weather",
                      flush=True)
                provider = None
                ws = None
        frames.append(project_year(config, y, ref, n_weeks=n_weeks, weather_shapes=ws))
    return pd.concat(frames, ignore_index=True)
