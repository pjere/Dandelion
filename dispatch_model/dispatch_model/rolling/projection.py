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
from ..res_schemes import load_res_schemes, solve_with_triggers
from ..rules import rules_at
from ..scheme_evolution import scheme_shares, trigger_hours
from ..tyndp import flex_capacity_mw, load_tyndp, tyndp_factors
from .assemble import _EXCLUDE_DISPATCH, NTC, flow_derived_ntc
from .windows import fr_stack_base, fr_window, nb_window

# default structural CAGRs (editable — dispatch_projection tab). RES build-out dominates the negative-price
# story; demand creeps up with electrification; coal/lignite phase down.
_GROWTH = {"demand": 0.008, "res": 0.045, "coal": -0.08, "lignite": -0.08}


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


def _append_flex(stack: pd.DataFrame, zone: str, tyndp: dict, year: int) -> pd.DataFrame:
    """Append the zone's 2040-flexibility fleet (battery + DR + H2-peaker) as one dispatchable block from the
    TYNDP `cap_flex_gw` trajectory (#83). Priced at its VOM (~€180) by `srmc`, it is the adequacy backstop
    that caps scarcity as firm thermal retires — no-op where TYNDP gives no flex for the zone."""
    mw = flex_capacity_mw(tyndp, zone, year) if tyndp else 0.0
    if mw < 50:
        return stack
    row = {"unit_id": f"{zone}_flex", "zone": zone, "tech": "flex", "capacity_mw": float(mw),
           "efficiency": np.nan, "min_gen_frac": 0.0}
    return pd.concat([stack, pd.DataFrame([{c: row.get(c, np.nan) for c in stack.columns}])], ignore_index=True)


def project_year(config: Config, target_year: int, ref, n_weeks: int | None = None,
                 avail_rng=None, weather_shapes: dict | None = None,
                 return_prices: bool = False) -> pd.DataFrame:
    """Clear `target_year` from the preloaded reference-year shapes in `ref`; return per-zone price stats."""
    zones, neigh, wb, cm, basis, floors, g = (ref[key] for key in
                                              ("zones", "neigh", "wb", "cm", "basis", "floors", "growth"))
    k = target_year - ref["ref_year"]
    # #76: per-zone demand/RES/capacity multipliers from TYNDP where available, else the flat CAGR fallback.
    tyndp = ref.get("tyndp") or {}

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
        storage_proj = storage_spec({}, target_year) or None
        for z, st_z in [("FR", fr_stack)] + list(nb_stack.items()):
            b = bess_power_mw(z, target_year)
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
    res_registry = ref.get("res_registry") or {}
    schemes = {z: scheme_shares(z, target_year, floors.get(z, {}), reg=res_registry.get(z))
               or ref["static"].get(z, []) for z in zones}
    if flex_spec is not None and "FR" in schemes:          # §6 (F4): FLEX owns the FR bid ladder. The OA
        from ..flexibility.trajectories import apply_oa_ladder, load_oa_ladder  # *volume* still decays by
        schemes["FR"] = apply_oa_ladder(schemes["FR"], load_oa_ladder(wb, target_year))  # vintage via scheme_shares

    price_chunks = []
    prev_flex_state = None; prev_w1 = None                     # F5: FR tail state across adjacent window seams
    for w0, w1 in zip(ref["weeks"][:-1], ref["weeks"][1:]):
        T = fr.loc[(fr.index >= w0) & (fr.index < w1)].index
        if len(T) < 24:
            prev_flex_state = None
            continue
        w0_t = w0 + pd.DateOffset(years=k)                     # commodity + market-rule year = the target
        prices = ref["resolver"].prices_at(w0_t)
        zd = {"FR": fr_window(fr, fr_stack,
                              zone_prices(prices, "FR", basis, w0_t, ref.get("gas_rules")), T)}
        for z in neigh:
            zd[z] = nb_window(z, nb_stack[z], nb_nl[z], ref["nb_res"][z],
                              zone_prices(prices, z, basis, w0_t, ref.get("gas_rules")), T)
        borders = [b for b in NTC if b[0] in zd and b[1] in zd]
        res_bid, price_floor = rules_at(wb, w0_t, list(zd))
        cold = seam = None
        if flex_spec is not None:
            cold = dict(flex_spec)
            seam = {**cold, **prev_flex_state} if (prev_flex_state is not None and prev_w1 == w0) else cold

        # seam-linked first, cold fallback if it over-constrains the window (see run_backtest for the rationale)
        out = None
        for sp in ([seam, cold] if seam is not cold else [seam]):
            try:
                out = solve_with_triggers(T, zd, borders, {b: ref["ntc"][b] for b in borders}, schemes,
                                          res_bid=res_bid, price_floor=price_floor,
                                          flex=({**{z: s for z, s in nb_flex.items() if z in zd},
                                                 "FR": sp} if sp is not None else None),
                                          fired_floor=fired_floor, storage=storage_proj)
                break
            except RuntimeError:
                out = None
        if out is None:
            prev_flex_state = None; continue
        price_chunks.append(out["prices"])
        if flex_spec is not None and out.get("flex", {}).get("FR") is not None:
            from ..flexibility.fr_nuclear import tail_state
            prev_flex_state = tail_state(out["flex"]["FR"]); prev_w1 = w1
        if n_weeks and len(price_chunks) >= n_weeks:
            break
    if not price_chunks:
        raise RuntimeError(f"projection {target_year}: every weekly LP window failed to solve")
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
    zones = [z for z in config.all_zones if z != "GB"]
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
    # water value — the projection was MISSING this entirely: `run_backtest` expands the single
    # `hydro_reservoir` block into the calibrated water-value tranches (backtest.py, `expand_stack`), but
    # `_preload` never did, so every projected year dispatched reservoirs at ~1 €/MWh VOM under a hard
    # ref-year energy budget — an all-or-nothing budget dual instead of a graded opportunity cost, i.e.
    # hydro dumping into cheap hours and no water-value support in the mid-band. Expanded here on the
    # REFERENCE year's curves (the same object the backtest calibrates); `project_year` then re-levels
    # them to the target year, since a 2019 water value belongs to a €39/MWh world.
    hydro_curves = {}
    try:
        from ..hydro.water_value import load_curves
        hydro_curves = load_curves(config, ref_year, tuple(["FR"] + list(neigh)))
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
    static = load_res_schemes(wb)
    from ..markup import load_model
    try:                                           # step-vii wedge; None → trajectories are raw SMC
        markup = load_model(config)
    except (FileNotFoundError, OSError):
        markup = None
    avail_stats = {}
    if avail_years:                                # #80: REMIT neighbour availability spread (opt-in; slow)
        from ..neighbour_availability import load_zone_stats
        avail_stats = load_zone_stats(neigh, avail_years)
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
            "avail_stats": avail_stats, "tyndp": load_tyndp(wb), "res_registry": res_registry,
            "cm": cm, "basis": load_zone_basis(wb), "resolver": PriceResolver(cm),
            "gas_rules": load_gas_rules(wb),
            "floors": {z: {t["scheme"]: t["floor"] for t in static.get(z, [])} for z in zones},
            "static": static, "growth": _load_growth(wb),
            "hydro_curves": hydro_curves,
            "fr": fr, "fr_stack": fr_stack_base(config, latest_fleet_year(config)), "nb_stack": nb_stack,
            "nb_nl": nb_nl, "nb_res": nb_res, "ntc": flow_derived_ntc(config, ref_year),
            "weeks": pd.date_range(f"{ref_year}-01-01", f"{ref_year + 1}-01-01", freq="7D", tz="UTC")}


def project_trajectory(config: Config, years: list[int], ref_year: int = 2019,
                       n_weeks: int | None = None) -> pd.DataFrame:
    """Price trajectory across `years` from a single reference-year preload."""
    ref = _preload(config, ref_year)
    return pd.concat([project_year(config, y, ref, n_weeks=n_weeks) for y in years], ignore_index=True)
