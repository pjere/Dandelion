"""Full-year rolling backtest: solve the multi-zone LP over weekly windows and score vs observed prices.

Year data is preloaded once (FR net load + per-zone neighbour net loads + block stacks + reservoir
generation + observed prices), so the ~52 weekly windows are fast LP solves. Each window uses that
month's commodity prices for SRMC and the window's actual reservoir energy as the hydro budget. Scores
the §8 price metrics per zone (baseload error, quantile errors, correlation, negative/spike frequency,
FR–DE spread) — the acceptance gate. Generation/flow physics metrics are a documented extension.
"""
from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

from powersim_core import lake

from ..commodities.gas_rules import load_gas_rules
from ..commodities.model import CommodityModel, load_zone_basis, zone_prices
from ..commodities.resolve import PriceResolver
from ..config import Config
from ..io.entsoe_hist import load_generation_hist
from ..io.fr_history import load_fr_netload
from ..neighbours.blocks import build_neighbour_stack, constituents, neighbour_netload
from ..res_schemes import load_res_schemes, solve_with_triggers
from ..rules import rules_at
from .assemble import _EXCLUDE_DISPATCH, NTC, flow_derived_ntc
from .windows import fr_stack_base, fr_window, nb_window


def _observed_prices(config, year, zones):
    con = sqlite3.connect(config.resolve(config.section("data")["sqlite_path"]))
    try:
        df = pd.read_sql("SELECT ts_utc, series_key, value FROM entsoe_day_ahead_prices "
                         "WHERE ts_utc >= ? AND ts_utc < ?",
                         con, params=(f"{year}-01-01", f"{year + 1}-01-01"))
    finally:
        con.close()
    df["timestamp_utc"] = pd.to_datetime(df["ts_utc"], utc=True)   # DB raw ts_utc → canonical
    out = {}
    for z in zones:
        s = df[df["series_key"] == z].set_index("timestamp_utc")["value"]
        out[z] = s[~s.index.duplicated()].resample("1h").mean() if not s.empty else None
    return out


def _fr_maneuverability(config, year, week_starts):
    """Per-week FR nuclear maneuverability from the REMIT refuelling calendar (C4/F1), as
    {week_start: {name: (state, stretch_power)}} keyed by plant name (= fleet ``name`` = REMIT ``unit_name``).
    Returns None when REMIT is unreadable/empty → every reactor stays ``full`` (a documented degrade)."""
    try:
        from pricemodeling.config import load_settings
        from pricemodeling.db import get_engine

        from ..flexibility import maneuverability as mv
        con = get_engine(load_settings().db_url)
        cal = mv.backtest_calendar(con, f"{year}-01-01", f"{year + 1}-01-01")
        if cal.empty:
            return None
        weekly = mv.derive_weekly(cal, mv.units_from_calendar(cal), week_starts)
    except Exception:                                    # noqa: BLE001 — no REMIT: degrade to full, don't fail
        return None
    out: dict = {}
    for r in weekly.itertuples(index=False):
        out.setdefault(r.week_start, {})[r.unit_id] = (r.maneuverability, float(r.stretch_power))
    return out or None


def run_backtest(config: Config, year: int, n_weeks: int | None = None,
                 use_remit_nuclear_avail: bool = False, de_unit_level: bool | None = None,
                 nuclear_curve: bool = True, hydro_sdp_level: bool = True,
                 diagnose: bool = False, flexibility: bool | None = None,
                 write_lake: bool = True, enable_storage: bool | None = None) -> dict:
    """`flexibility` opts into the FLEX plant-operating-rigidity module (per-reactor FR nuclear stack with
    C1/C2/C3/C5 rigidities → endogenous negatives; see ``flexibility.fr_nuclear``). Default None reads
    ``flexibility.enabled`` from config.yaml (off unless set). When on, the FR nuclear tranche surrogate
    (`nuclear_curve`) is bypassed — the two are mutually exclusive representations of the same fleet.

    `write_lake=False` skips persisting prices/metrics — REQUIRED for calibration sweeps (F7), which must
    never overwrite the golden `backtest_prices` artifact with partial or experimental runs."""
    zones = [z for z in config.all_zones if z != "GB"]
    neigh = [z for z in zones if z != "FR"]
    wb = config.resolve(config.section("assumptions")["workbook"])
    cm = CommodityModel.from_workbook(wb)
    basis = load_zone_basis(wb)                                 # per-zone gas hub (PSV/MIBGAS vs TTF)
    # Real dated fuel/ETS prices where they have been ingested, scenario trajectory otherwise. With an
    # empty observed store this resolves exactly to the old `_month_prices` path (byte-identical).
    resolver = PriceResolver(cm)
    gas_rules = load_gas_rules(wb)          # hub basis + Iberian gas-for-power cap (RDL 10/2022)
    res_schemes = load_res_schemes(wb)                          # RES subsidy bid tranches per zone (§51)

    from ..flexibility import enabled as _flex_enabled
    flex_on = _flex_enabled(config) if flexibility is None else bool(flexibility)
    if flex_on:
        use_remit_nuclear_avail = True         # FLEX needs the TRUE nuclear envelope, not the output proxy (F7)
    if de_unit_level is None:
        de_unit_level = False                  # unit-level DE stays explicit opt-in: #73 validated it on 2019,
        #                                        but under FLEX-2024 it over-prices DE (mean 79 vs 65 obs) and
        #                                        drains the regional surplus (A/B: FR 136→59, BE 67→5 negs)

    # ---- preload the year ----
    fr = load_fr_netload(config, f"{year}-01-01", f"{year + 1}-01-01").set_index("timestamp_utc")
    fr_stack = fr_stack_base(config, year)
    nb_stack = {}
    for z in list(neigh):
        try:                                    # DE_REST (virtual NL+AT+DK+PL+CZ) only has generation for
            if de_unit_level and z == "DE_LU":  # #73: unit-level DE thermal from the MaStR registry
                from ..neighbours.blocks import build_de_unit_stack
                nb_stack[z] = build_de_unit_stack(config, z, year)
            else:
                nb_stack[z] = build_neighbour_stack(config, z, year)   # 2019; drop it (and its borders) in years
        except (KeyError, ValueError):          # it lacks data rather than failing the whole backtest.
            neigh.remove(z)
            zones.remove(z)
    psp_mw = {z: float(s.loc[s["tech"] == "hydro_psp", "capacity_mw"].sum()) for z, s in nb_stack.items()}
    nb_stack = {z: s[~s["tech"].isin(_EXCLUDE_DISPATCH)].reset_index(drop=True) for z, s in nb_stack.items()}
    # valeur de l'eau : le bloc hydraulique unique a 1 EUR/MWh devient une courbe de tranches calibree,
    # calculee une fois par annee (cf. hydro.water_value)
    from ..hydro.water_value import expand_stack, load_curves
    curves = load_curves(config, year, tuple(["FR"] + list(neigh)))
    fr_stack = expand_stack(fr_stack, curves, "FR")
    nb_stack = {z: expand_stack(s, curves, z) for z, s in nb_stack.items()}
    # synthese SDP x empirique (#136) : le niveau de la courbe hydro vient du lambda structurel de Bellman,
    # la dispersion reste empirique. Decalage par (zone, semaine) applique dans les fenetres. Opt-in.
    wv_levels = {}
    if hydro_sdp_level:
        from ..hydro.synthesis import solve_levels
        wv_levels = solve_levels(config, year, curves, tuple(["FR"] + list(neigh)))
    # meme traitement pour le nucleaire FR : 63 GW a un prix unique rendaient le prix francais degenere
    # (marginal 78,6 % des heures a exactement 7,0 EUR/MWh). Cf. stacks.nuclear_curve.
    from ..stacks import nuclear_curve as nuc
    nuc_installed = float(fr_stack.loc[fr_stack["tech"] == "nuclear", "capacity_mw"].sum())
    flex_spec = None
    nb_flex: dict = {}                                     # neighbour-zone flex specs (static per year)
    nb_mustrun: dict = {}                                  # measured must-run floors (flex-gated, DE)
    storage_lp: dict = {}                                  # PSP+BESS storage spec (flex-gated)
    fired_floor = 0.0                                      # flag-off: historic §51 fired-tranche floor
    if flex_on:
        # FLEX on: keep the per-reactor rows and build the C1–C7/§4 rigidity spec — the negatives now emerge
        # from the rigidity, so the tranche surrogate (which bakes a −40 socle bid in) is bypassed. F3 adds
        # reserves (C6), the grid-stability floor (C7) and fossil commitment (§4); maneuverability (C4) is
        # applied per window below.
        from ..flexibility import fr_nuclear, trajectories
        costs = trajectories.load_flex_costs(wb, year)
        reserves = trajectories.load_reserves(wb, year)
        fr_stack, flex_spec = fr_nuclear.build_flex_spec(
            fr_stack, nuc.load_curve(config, year, nuc_installed),
            c_mod=costs["c_mod"], c_start_by_class=costs,
            r_up_req=reserves["r_up_req"], r_down_req=reserves["r_down_req"],
            p_minstab=trajectories.minstab_mw(wb, "FR", year),
            include_fossil=True, fossil_c_start=costs)
        ladder = trajectories.load_oa_ladder(wb, year)
        if "FR" in res_schemes:                            # §6 (F4): the FLEX module owns the FR downward
            res_schemes = {**res_schemes,                  # bid ladder — OA at the market floor, CR ≈0
                           "FR": trajectories.apply_oa_ladder(res_schemes["FR"], ladder)}
        fired_floor = 0.0                                  # fired §51 tranches bid the German-law 0.0: fired
        #                                                    hours clear AT zero (reality prints 0.00), and the
        #                                                    −0.01 variant mass-printed phantom negatives
        #                                                    (A/B: DE 545 vs 70 obs). The F7 FR unlock was the
        #                                                    trigger fix, not the fired level (65=65 at 0.0).
        # year-correct DE tranche volumes: the static tab is a 2019 snapshot, but German FiT volumes shrank
        # sharply by 2024 (registry vintage decay: fit 0.30→0.18, merchant 0.10→0.20, §51 trigger 6→4 h) —
        # oversized deep floors over-print DE (probe: 303 vs 70 obs). DE_LU ONLY: its registry is plant-level
        # MaStR and §51 trigger semantics are genuinely German; BE/CH/ES keep the static tab (their cohort
        # registry tier is degenerate single-scheme, and scheme_shares would bolt a German trigger onto
        # paid-regardless certificate schemes that have none).
        if "DE_LU" in res_schemes:
            try:
                from powersim_core import registry as _registry

                from ..scheme_evolution import scheme_shares
                de_floors = {t["scheme"]: t["floor"] for t in res_schemes["DE_LU"]}
                ys = scheme_shares("DE_LU", year, de_floors, reg=_registry.read(zone="DE_LU"))
                if ys:
                    res_schemes = {**res_schemes, "DE_LU": ys}
            except Exception:                              # noqa: BLE001 — registry unavailable → static tab
                pass
        # neighbour-zone extension: pseudo-unit nuclear rigidity for BE/CH/ES (+DE 2019), measured per-zone
        # anchors (near-must-run fleets, socle bids −55…−70) — the coupled mid-band depth the F7 report
        # flagged as missing; DE thermal gets §4 commitment on its MaStR unit stack.
        from ..flexibility import neighbour_nuclear as nnuc
        for z in list(nb_stack):
            st_z = nnuc.split_nuclear_block(nb_stack[z], z)
            spec_z = nnuc.build_neighbour_flex_spec(st_z, z, costs)
            if z == "DE_LU" and de_unit_level:
                if spec_z is None:                         # 2024+: no German nuclear → standalone §4 fossil
                    spec_z = nnuc.build_fossil_flex_spec(st_z, costs)
                else:                                      # 2019: nuclear + unit-level fossil combined
                    fr_nuclear._append_fossil(st_z, spec_z, costs)
            if spec_z is not None:
                nb_stack[z] = st_z
                nb_flex[z] = spec_z
    elif nuclear_curve:
        fr_stack = nuc.expand_stack(fr_stack, nuc.load_curve(config, year, nuc_installed))
    nb_nl = {z: neighbour_netload(config, z, year).set_index("timestamp_utc") for z in neigh}
    for z in list(neigh):                       # a zone with load data missing for this year → drop it (else
        if nb_nl[z].empty:                      # its empty net-load yields a degenerate LP time coord)
            neigh.remove(z); zones.remove(z); nb_stack.pop(z, None); nb_nl.pop(z, None)
    nb_res = {}
    nb_gen = {}
    for z in neigh:
        g = load_generation_hist(config, year, zones=constituents(z))   # virtual zones sum constituents
        nb_gen[z] = g
        res_g = g[g["tech"] == "hydro_reservoir"]
        nb_res[z] = (res_g.groupby("timestamp_utc")["gen_mw"].sum()
                     if not res_g.empty else pd.Series(dtype=float))
    obs = _observed_prices(config, year, zones)
    if flex_on:
        # RES-potential reconstruction (flex-gated, NEIGHBOURS only, solar only): must-take RES = observed
        # generation = post-curtailment, which understates the surplus exactly on the hours that price
        # negative. Envelope-based solar uplift, price-unconditioned per hour (see flexibility.res_potential
        # for the estimator and its leakage boundary). FR is deliberately excluded in v1 (its negative count
        # already runs above observed; an uplift there would compound the overshoot).
        from ..flexibility.res_potential import btm_solar, solar_uplift
        for z in neigh:
            up = solar_uplift(nb_gen[z], obs.get(z))
            if not up.empty and float(up.sum()) > 0:
                nl = nb_nl[z]
                add = up.reindex(nl.index).fillna(0.0)
                nl["musttake_res_mw"] = nl["musttake_res_mw"] + add
        # NL behind-the-meter PV (flex-gated): ~98 % of the Dutch solar fleet (29.3 GW installed 2025,
        # 0.5 TWh/yr metered) is invisible on BOTH sides of the ENTSO-E balance — not in generation
        # (behind-the-meter) and not netted from the load series (measured: net load does not collapse
        # on observed-negative noons). Without it the real Dutch surplus does not exist in the inputs
        # (2025 decomposition: model-NL ~88 €/MWh on obs-negative hours, gas marginal 470/581 h).
        if "NL" in neigh:
            from ..io.entsoe_hist import load_installed_capacity
            inst = load_installed_capacity(config, "NL", year).get("solar", 0.0)
            btm = btm_solar(nb_gen["NL"], inst)
            if not btm.empty and float(btm.sum()) > 0:
                nl = nb_nl["NL"]
                nl["musttake_res_mw"] = nl["musttake_res_mw"] + btm.reindex(nl.index).fillna(0.0)
                nl["netload_mw"] = nl["load_mw"] - nl["musttake_res_mw"]
        # measured must-run floors (flex-gated): p10 of observed generation per tech-month replaces the
        # chp×heat_factor heuristic, which forced ~10 GW of phantom German gas on surplus hours — the
        # dominant share of DE's long bias (see blocks.observed_mustrun_floors).
        from ..neighbours.blocks import measured_chp_mw, observed_mustrun_floors, participation_caps
        nb_mustrun = {z: observed_mustrun_floors(config, z, year) for z in neigh if measured_chp_mw(z)}
        # revealed thermal participation (flex-gated): nameplate × 0.95 offers phantom capacity — the
        # market fleet saturates at the annual p99.9 of observed generation (validated as a true
        # ceiling on >150/>200 €/MWh hours: DE gas 0.51 of nameplate, ES 0.50, NL 0.60, BE 0.66;
        # the excess is mothballed/Netzreserve stock that cleared every model scarcity hour at
        # mid-stack SRMC). Clamp each thermal tech's block rows to min(nameplate, revealed ceiling).
        for z in neigh:
            caps = participation_caps(config, z, year)
            st = nb_stack[z]
            for tech, ceil in caps.items():
                rows = st["tech"] == tech
                tot = float(st.loc[rows, "capacity_mw"].sum())
                if tot > ceil > 0:
                    st.loc[rows, "capacity_mw"] *= ceil / tot
        # storage in the LP (`enable_storage=None` → ON under flex since the 2025 re-gate): PSP from
        # the measured envelopes + BESS 2024 seeds (flexibility.storage). History: at nameplate
        # parameters the frictionless arbitrage ANNIHILATED the thin 2024-era negative tail (probe I:
        # DE 70→0) and storage stayed opt-in — but once the surpluses became realistic (measured
        # ladders, BTM-NL, participation ceilings), the re-gate PASSED decisively: FR 530/510 strict
        # and 1042/1066 boundary (essentially exact), CH scarcity 248→39/52 and IT 192→34/43 (the
        # excluded PSP was exactly their over-print), no annihilation (FR keeps 530 vs the 3 of the
        # 2024 probes). Known residual: discharge is still frictionless — observed PSP discharge
        # utilization is only 32–54 % in the top price quartile (psp_envelopes) and the discharge-side
        # derate is NOT yet encoded, so model peaks are shaved slightly too hard (DE 44 vs 162 h >200).
        if enable_storage or (enable_storage is None and flex_on):
            from ..flexibility.storage import storage_spec
            from ..io.entsoe_hist import load_installed_capacity
            psp_mw["FR"] = float(load_installed_capacity(config, "FR", year).get("hydro_psp", 0.0))
            storage_lp = storage_spec(psp_mw, year)
    ntc = flow_derived_ntc(config, year)                        # effective NTC from realized flows
    # regime-conditional caps (flex-gated): static caps over-couple exactly on surplus hours — the
    # flow-based exchange domain shrinks when RES is high; measured collapse + design in
    # assemble.regime_ntc. Applied per window/hour via regime_cap_arrays.
    rn = None
    if flex_on:
        from .assemble import regime_ntc
        try:
            rn = regime_ntc(config, year, ntc)
        except Exception:                              # noqa: BLE001 — thin data → static caps
            rn = None
    nuc_unavail = None
    if use_remit_nuclear_avail:                                 # #78: true FR nuclear availability from REMIT
        from pricemodeling.config import load_settings
        from pricemodeling.db import get_engine
        from pricemodeling.entsoe.unavailability import nuclear_unavailable_mw
        nu = nuclear_unavailable_mw(get_engine(load_settings().db_url), "FR", year)
        nuc_unavail = nu if not nu.empty else None

    # ---- weekly windows ----
    weeks = pd.date_range(f"{year}-01-01", f"{year + 1}-01-01", freq="7D", tz="UTC")
    if n_weeks:
        weeks = weeks[:n_weeks + 1]
    # C4 maneuverability: per-week {name: (state, stretch_power)} from the REMIT refuelling calendar (None
    # when REMIT is unavailable → every reactor `full`). Applied to the base flex spec window by window.
    maneuver_weekly = _fr_maneuverability(config, year, weeks[:-1]) if flex_spec is not None else None
    price_chunks, diag_chunks, flow_chunks = [], [], []
    flex_stats = {"modulated_mwh": 0.0, "deepmod_mwh": 0.0, "hours": 0}   # F7 §9: fleet modulation aggregates
    prev_flex_state = None                                       # F5: previous window's tail state (FR), or None
    prev_w1 = None                                               # end of the previous window (seam-adjacency check)
    for w0, w1 in zip(weeks[:-1], weeks[1:]):
        T = fr.loc[(fr.index >= w0) & (fr.index < w1)].index
        if len(T) < 24:
            prev_flex_state = None
            continue
        prices = resolver.prices_at(w0)
        wk = int(pd.Timestamp(w0).isocalendar().week)          # semaine ISO pour le decalage SDP
        zd = {"FR": fr_window(fr, fr_stack, zone_prices(prices, "FR", basis, w0, gas_rules), T,
                          nuc_unavail_daily=nuc_unavail, wv_delta=wv_levels.get("FR", {}).get(wk))}
        for z in neigh:
            zd[z] = nb_window(z, nb_stack[z], nb_nl[z], nb_res[z],
                              zone_prices(prices, z, basis, w0, gas_rules), T,
                              wv_delta=wv_levels.get(z, {}).get(wk),
                              mustrun_floors=nb_mustrun.get(z))
        borders = [b for b in NTC if b[0] in zd and b[1] in zd]
        # market rules effective in THIS window (IT/ES were floored at 0 before TIDE / Dec-2023)
        res_bid, price_floor = rules_at(wb, w0, list(zd))
        cold = seam = None
        if flex_spec is not None:
            from ..flexibility import fr_nuclear
            # multi-zone flex dict: FR (weekly C4 re-derate) + the static neighbour specs; F5 seam state
            # per zone, linked only across an adjacent seam.
            cold = {"FR": fr_nuclear.window_spec(flex_spec, (maneuver_weekly or {}).get(w0)),
                    **{z: nb_flex[z] for z in nb_flex if z in zd}}
            adjacent = prev_flex_state is not None and prev_w1 == w0
            seam = ({z: {**sp, **prev_flex_state[z]} if z in prev_flex_state else sp
                     for z, sp in cold.items()} if adjacent else cold)

        # try the seam-linked spec first; a seam link can over-constrain a window (a last-hour commitment shed
        # upstream + hard reserves) → fall back to a cold solve (F4 behaviour) rather than drop the window.
        if rn is not None:
            from .assemble import regime_cap_arrays
            ntc_w = regime_cap_arrays(borders, ntc, rn, T)
        else:
            ntc_w = {b: ntc[b] for b in borders}
        out = None
        for sp in ([seam, cold] if seam is not cold else [seam]):
            try:
                out = solve_with_triggers(T, zd, borders, ntc_w, res_schemes,
                                          res_bid=res_bid, price_floor=price_floor, diagnose=diagnose,
                                          flex=sp, fired_floor=fired_floor,
                                          storage=(storage_lp or None))
                break
            except RuntimeError:
                out = None
        if out is None:
            prev_flex_state = None
            continue
        price_chunks.append(out["prices"])
        if diagnose and out.get("diag") is not None:
            diag_chunks.append(out["diag"])
            if out.get("diag_flows") is not None:
                flow_chunks.append(out["diag_flows"])
        fx = out.get("flex", {}).get("FR") if flex_spec is not None else None
        if fx is not None:
            nucm = np.asarray(flex_spec.get("is_nuclear", np.ones(len(fx["idx"]), bool)), bool)
            # modulated energy = Σ(u − p) over committed nuclear: capacity held on but not producing — the
            # model analogue of EDF's fleet "énergie modulée" (~30–33 TWh/yr, the §9 level target).
            flex_stats["modulated_mwh"] += float((fx["u"][nucm] - fx["p"][nucm]).clip(min=0.0).sum())
            flex_stats["deepmod_mwh"] += float(fx["d"][nucm].sum())
            flex_stats["hours"] += int(fx["u"].shape[1])
        prev_flex_state = ({z: fr_nuclear.tail_state(v) for z, v in out.get("flex", {}).items()}
                           if flex_spec is not None and out.get("flex") else None)   # F5 tails, per zone
        prev_w1 = w1

    model = pd.concat(price_chunks).sort_index()
    metrics = _score(model, obs, zones)
    if write_lake:
        outdir = config.reports_dir
        outdir.mkdir(parents=True, exist_ok=True)
        lake.write_table(model, "dispatch", "backtest_prices", year=year)
        metrics.to_csv(outdir / f"backtest_{year}_metrics.csv", index=False)   # CSV = human export (§6)
    res = {"model_prices": model, "observed": obs, "metrics": metrics}
    if flex_stats["hours"]:                     # F7: fleet modulation aggregates (see §9 calibration targets)
        res["flex_stats"] = {k: float(v) for k, v in flex_stats.items()}
    if diagnose:                                # lecture de la solution primale (cf. lp.diagnostics), opt-in
        res["diag"] = pd.concat(diag_chunks, ignore_index=True) if diag_chunks else pd.DataFrame()
        res["diag_flows"] = pd.concat(flow_chunks, ignore_index=True) if flow_chunks else pd.DataFrame()
    return res


def _score(model, obs, zones) -> pd.DataFrame:
    rows = []
    for z in zones:
        o = obs.get(z)
        if o is None:
            continue
        m = model[z]
        idx = m.index.intersection(o.index)
        m, o = m.reindex(idx), o.reindex(idx)
        ok = m.notna() & o.notna()
        m, o = m[ok], o[ok]
        if len(m) < 100:
            continue
        rows.append({
            "zone": z, "hours": len(m),
            "model_mean": round(m.mean(), 1), "obs_mean": round(o.mean(), 1),
            "baseload_err_pct": round(100 * (m.mean() - o.mean()) / o.mean(), 1),
            "corr": round(float(np.corrcoef(m, o)[0, 1]), 3),
            "P5_err": round(m.quantile(.05) - o.quantile(.05), 1),
            "P50_err": round(m.quantile(.50) - o.quantile(.50), 1),
            "P95_err": round(m.quantile(.95) - o.quantile(.95), 1),
            "neg_hrs_model": int((m < 0).sum()), "neg_hrs_obs": int((o < 0).sum()),
            "spike_hrs_model": int((m > 3 * o.median()).sum()), "spike_hrs_obs": int((o > 3 * o.median()).sum()),
        })
    return pd.DataFrame(rows)
