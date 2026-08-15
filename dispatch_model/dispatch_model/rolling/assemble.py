"""Assemble per-zone LP inputs for a backtest window (FR unit-level + neighbour blocks).

Handles the double-count traps: run-of-river and solar/wind are must-take (res_pot, not dispatchable);
pumped storage is excluded from v1 dispatch (storage arbitrage is a refinement). Reservoir hydro is
bid ~0 with a weekly energy budget = the window's ACTUAL reservoir generation (so hydro energy is
anchored to history while the LP places it optimally → endogenous water value). FR nuclear availability
is a rolling-max-of-output proxy; neighbour block capacities are already an availability proxy (p99 of
observed generation), so their availability is ~1.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import xarray as xr

from ..commodities.model import CommodityModel
from ..config import Config
from ..io.fr_history import load_fr_netload
from ..neighbours.blocks import build_neighbour_stack, constituents, neighbour_netload
from ..stacks.fr_stack import build_fr_stack, srmc

# representative NTC (MW) per border (ntc_ab, ntc_ba); workbook/ENTSO-E override later
NTC = {
    ("FR", "DE_LU"): (3000, 3000), ("FR", "BE"): (4300, 2800), ("FR", "CH"): (3000, 3000),
    ("FR", "IT_NORTH"): (4350, 2650), ("FR", "ES"): (2800, 3300), ("DE_LU", "BE"): (1000, 1000),
    ("DE_LU", "CH"): (4000, 4000), ("CH", "IT_NORTH"): (4200, 1900),
    # DE-LU's out-of-model neighbours, split into four clusters (was one DE_REST block). Defaults are
    # physical interconnector capacities; `flow_derived_ntc` recomputes per backtest year from realized
    # flow (summing constituent borders), so these only bind in years lacking flow history.
    ("DE_LU", "NL"): (4250, 4250), ("BE", "NL"): (2400, 2400),          # NL cluster
    ("DE_LU", "DK"): (3500, 3500),                                       # DK_1 + DK_2 → DE
    ("DE_LU", "PL_CZ"): (2600, 2600),                                    # PL + CZ → DE
    ("DE_LU", "AT_SI"): (5400, 5400), ("CH", "AT_SI"): (1200, 1200),    # AT + SI: DE / CH / IT-North
    ("IT_NORTH", "AT_SI"): (870, 385),                                   # Brenner (AT) + IT↔SI
    # IT-North ↔ the southern Italian bidding zones (internal NORD↔CNOR border). Gives IT-North its
    # export-south demand — the missing tightness that under-priced it ~15 % in 2024 (#142).
    ("IT_NORTH", "IT_SOUTH"): (5000, 3000),
    # ES ↔ PT. Spain had the same defect as IT-North and worse: modelled as an island whose only outlet
    # was the Pyrenees. Measured on 2024, Spain's balance residual (generation − Spanish demand − the
    # French exchange) is +2.4 GW on average and +4.1 GW in the hours priced below 5 EUR/MWh — four times
    # what crosses to France, which itself sits at 1.0 GW against a 2.8 GW NTC in those hours, i.e. the
    # French border was not even binding. With nowhere to send the surplus the LP drove the Spanish price
    # negative: 1329 negative hours in backtest against 247 observed, and the whole ES distribution ~16
    # EUR/MWh low. Physical ES↔PT capacity is ~4.2 GW, larger than ES↔FR; `flow_derived_ntc` recomputes
    # it per year from realised flow, so this default only binds in years lacking flow history.
    ("ES", "PT"): (4200, 3500),
    # GB's borders, added when GB was promoted out of `BORDER_ONLY_ZONES`. Values are the sum of the
    # interconnectors on each border, and every one was MEASURED from Elexon FUELINST rather than quoted:
    # p99.5 of hourly flow over 2024, import / export MW, against nameplate in brackets —
    #     IFA 2008/1955 (2000) + IFA2 992/886 (1000) + ElecLink 999/705 (1000) → FR 4000
    #     Nemo 1019/1021 (1000) → BE           BritNed 1055/1047 (1000) → NL
    # Applied SYMMETRICALLY even though the measured export p99.5 on the French links is lower (2683 for
    # the three combined, against 3994 importing): the export side is dispatch behaviour, not a limit. GB
    # simply exports to France less often than it imports. That these are physically symmetric is visible
    # in the links where both directions are exercised — Nemo 1019/1021, BritNed 1055/1047 — and in IFA's
    # maximum export of 2070 MW, above its own nameplate.
    ("FR", "GB"): (4000, 4000), ("BE", "GB"): (1000, 1000), ("NL", "GB"): (1000, 1000),
    # Viking Link (GB↔DK_1). ZERO is deliberate and is not a missing number: the link commissioned
    # 2023-12-29, so in the 2019 and 2022 gate years it did not exist. ENTSO-E publishes no Viking series
    # at all, so unlike every other border here the static default would bind in EVERY year rather than
    # only those lacking flow history — asserting 1400 MW would put a phantom 1.4 GW link into two gate
    # years. `elexon.series.ingest_flows` supplies the realised flow from 2023 on, which `flow_derived_ntc`
    # turns into the real capacity for the years the link has run (measured p99.5 1424/1097 MW on 2024).
    ("DK", "GB"): (0, 0),
}
_EXCLUDE_DISPATCH = {"hydro_psp", "hydro_ror", "solar", "wind_onshore", "wind_offshore", "waste"}

#: Zones configured but NOT given a balance in the LP — they enter only as border tranches.
#:
#: EMPTY since GB was promoted. GB was the only member: it left ENTSO-E Transparency after Brexit (data
#: moved to Elexon/BMRS) and so had no load or generation history to build a stack from, and `DECISIONS.md`
#: recorded the call to carry it as a border supply/demand curve instead. `pricemodeling.elexon` now
#: sources GB generation, demand and prices, `pricemodeling.fx` converts BMRS's GBP to the lake's EUR, and
#: the `NTC` table above carries GB's four borders — so the reason for the exception is gone.
#:
#: The mechanism stays because it is the right shape for the next zone that has borders but no balance, and
#: because `modelled_zones` reads it. Adding a member changes the LP topology of every window, so a change
#: here requires the multi-year gate to be re-run, not just the edit.
#:
#: What GB's promotion does NOT fix: NSL to Norway (1.4 GW) and Moyle/EWIC/Greenlink to Ireland (1.4 GW)
#: connect GB to zones the model does not carry, so ~2.8 GW of its interconnection is still invisible.
#: Measured on 2024 those two nearly cancel — Norway supplies +1095 MW mean, Ireland takes −590 MW, a net
#: +505 MW or 1.8 % of GB demand — which is why the omission is tolerable, not why it is absent.
BORDER_ONLY_ZONES: tuple[str, ...] = ()

#: Zones given the MEASURED must-run floor (p10 of observed generation per tech-month) on top of the
#: `measured_chp_mw` selection, which only picks zones that happen to have a CHP entry — a proxy for
#: "this zone's flat `min_gen_frac` overstates its real floor", not the thing itself.
#:
#: ES was excluded by that proxy and has the defect anyway. Measured on 2024: 26.9 GW of Spanish gas at a
#: flat 0.15 `min_gen_frac` forces 4033 MW to run, against an observed p10 of 2440 MW and 1779 MW in
#: April. That ~1.6 GW of phantom forced supply lands on exactly the surplus hours that set the Spanish
#: price — the same failure `observed_mustrun_floors` was written for on DE (12.1 GW heuristic floor vs
#: 2.1-2.4 GW actually running, "the dominant share of DE's long bias").
MEASURED_MUSTRUN_ZONES = ("ES",)


def hourly_ntc(config, year: int, default: dict | None = None) -> dict:
    """→ {(a, b): (fwd_series, bwd_series)} of PUBLISHED hourly day-ahead NTC, per border/direction.

    `flow_derived_ntc` gives one scalar per direction for a whole year, and that cannot represent a border
    that closes. Measured on 2024 against the published series (`entsoe_ntc`, ~1.0 M rows):

        direction      model   real p50   real p10   min   hours at 0
        DE_LU>CH        2738        950        800     0            1
        BE>NL           1163        619        124     0          582
        CH>IT_NORTH     2469       2748       1622     0          265
        FR>IT_NORTH     1977       2622       1800     0           55

    The model's constant sits ABOVE the real p10 on all 16 directions measured, and 13 of them reach
    0 MW — the model allows full flow in every one of those hours. That is what couples IT_NORTH to a
    collapsing France: 1977 MW permitted hourly, against 187 MW actually delivered in the 277 hours the
    model prices Italy under +5 with a 69.5 EUR/MWh spread standing. An arbitrage that large across an
    open wire is impossible, so the wire was shut.

    Note the error runs BOTH ways — FR>IT_NORTH's real median (2622) is above the model's constant while
    its p10 (1800) is below — which is what a constant does to a quantity varying 4-10x. So this should
    loosen tight hours as well as tightening the closed ones.

    The published value is used AS PUBLISHED, without the per-zone coincidence factor `flow_derived_ntc`
    applies. That factor exists because summing per-border p99.5 overstates a zone's simultaneous export;
    published day-ahead NTCs are computed by the TSOs against the network and the market clears all
    borders together, so they should already be simultaneity-consistent. That is a judgement, not a
    measurement — the multi-year gate is what tests it.

    `default` (typically `flow_derived_ntc(...)`) fills any border/hour the publication does not cover.
    The CORE flow-based region (FR-DE_LU and neighbours) publishes no NTC at all, so those borders keep
    their derived scalar and nothing regresses where the series is absent.
    """
    import sqlite3
    con = sqlite3.connect(config.resolve(config.section("data")["sqlite_path"]))
    try:
        df = pd.read_sql("SELECT ts_utc, series_key, value FROM entsoe_ntc "
                         "WHERE ts_utc >= ? AND ts_utc < ?",
                         con, params=(f"{year}-01-01", f"{year + 1}-01-01"))
    except Exception:                                      # noqa: BLE001 — table absent → all defaults
        return dict(default or {})
    finally:
        con.close()
    out = dict(default or {})
    if df.empty:
        return out
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    piv = df.pivot_table(index="ts", columns="series_key", values="value", aggfunc="mean")
    piv = piv.resample("1h").mean()
    for key in list(out) + [tuple(k.split(">")) for k in piv.columns if ">" in k]:
        a, b = key
        f, w = f"{a}>{b}", f"{b}>{a}"
        if f not in piv.columns and w not in piv.columns:
            continue
        dflt = out.get((a, b)) or out.get((b, a)) or (0.0, 0.0)
        d0 = float(dflt[0]) if not hasattr(dflt[0], "__len__") else float(np.mean(dflt[0]))
        d1 = float(dflt[1]) if not hasattr(dflt[1], "__len__") else float(np.mean(dflt[1]))
        fwd = piv[f].fillna(d0) if f in piv.columns else None
        bwd = piv[w].fillna(d1) if w in piv.columns else None
        out[(a, b)] = (fwd if fwd is not None else d0, bwd if bwd is not None else d1)
    return out


def joint_export_enabled() -> bool:
    """Opt-in for the per-zone simultaneous-export row (`lp.highs_solver`). Read at each call site."""
    import os
    return os.environ.get("DISPATCH_JOINT_EXPORT", "0") not in ("0", "false", "False")


def published_directions(config: Config, year: int) -> set:
    """→ {(model_zone_a, model_zone_b)} directed pairs that carry a PUBLISHED day-ahead NTC series.

    `hourly_ntc` overrides the derived scalar wherever such a series exists, so those directions already
    carry a commercial authority and must be left out of the joint rows — see `zone_transfer_caps`.
    """
    import sqlite3
    con = sqlite3.connect(config.resolve(config.section("data")["sqlite_path"]))
    try:
        df = pd.read_sql("SELECT DISTINCT series_key FROM entsoe_ntc WHERE ts_utc >= ? AND ts_utc < ?",
                         con, params=(f"{year}-01-01", f"{year + 1}-01-01"))
    except Exception:                                    # noqa: BLE001 — no table → nothing published
        return set()
    finally:
        con.close()
    owner = {c: z for z in {zz for bd in NTC for zz in bd} for c in constituents(z)}
    out = set()
    for k in df["series_key"]:
        a, _, b = str(k).partition(">")
        za, zb = owner.get(a.strip()), owner.get(b.strip())
        if za and zb and za != zb:
            out.add((za, zb))
    return out


def zone_transfer_caps(config: Config, year: int) -> dict:
    """→ {zone: {"export": MW, "import": MW, "borders": [names]}} for the joint transfer rows.

    A zone's borders share internal network elements, so what limits simultaneous transfer is a shared
    element and the constraint belongs on the SUM. `flow_derived_ntc` approximates that with one
    coincidence factor per zone applied to every border, which is a BOX approximation to a SIMPLEX: it
    forbids one corridor running at its own revealed capability while the others idle. Measured on
    IT-North, whose NORD->CNOR corridor carries 91 % of its exports and is ANTI-correlated with the rest
    (p99.5 4210 MW, derated to 1843, exceeded in 41.8 % of 2024 hours) — northern Italy imports 6470 MW
    across the Alps while exporting 4146 MW south in its top-100 export hours, so it TRANSITS rather than
    competing for one budget.

    RESTRICTED TO BORDERS WITH NO PUBLISHED NTC, and that restriction is the whole lesson of the wide
    version. `hourly_ntc` already overrides the derived scalar wherever ENTSO-E publishes a day-ahead
    series, so IT-North's Alpine borders never moved at all when the coincidence factor came off
    (IT_NORTH-CH stayed 1810/2748, IT_NORTH-FR 1995/2622). What DID move was eleven unrelated interior
    borders that have no published series — FR-BE +2010/+1856, FR-DE_LU +1977, DE_LU-PL_CZ +1752,
    FR-GB +1771 — where the coincidence factor was doing legitimate work. The wide version therefore
    improved coupling SHAPE (pooled log_err 0.650 -> 0.628, better in 5 of 8 zones and all 4 years, best
    scarcity recall of any arm) while degrading LEVELS (|mean err| 10.50 -> 11.71), and IT-North itself
    got cheaper rather than dearer because north-west Europe loosened around it.

    So the rows bound exactly the residue that has no authority of its own, and their RHS is the p99.5 of
    the simultaneous total over THOSE legs only. Use with `flow_derived_ntc(..., coincident=False)`.
    """
    import sqlite3
    con = sqlite3.connect(config.resolve(config.section("data")["sqlite_path"]))
    try:
        df = pd.read_sql("SELECT ts_utc, series_key, value FROM entsoe_flows "
                         "WHERE ts_utc >= ? AND ts_utc < ?",
                         con, params=(f"{year}-01-01", f"{year + 1}-01-01"))
    finally:
        con.close()
    if df.empty:
        return {}
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    piv = df.pivot_table(index="ts", columns="series_key", values="value").fillna(0.0)
    pub = published_directions(config, year)

    def flow_series(x, y):
        cols = [f"{i}>{j}" for i in constituents(x) for j in constituents(y) if f"{i}>{j}" in piv.columns]
        return piv[cols].sum(axis=1) if cols else pd.Series(0.0, index=piv.index)

    out: dict = {}
    for z in {zz for bd in NTC for zz in bd}:
        neigh = [b for (a, b) in NTC if a == z] + [a for (a, b) in NTC if b == z]
        ex_w = [w for w in neigh if (z, w) not in pub]        # unpublished EXPORT directions only
        im_w = [w for w in neigh if (w, z) not in pub]        # unpublished IMPORT directions only
        ex = sum((flow_series(z, w) for w in ex_w), start=pd.Series(0.0, index=piv.index))
        im = sum((flow_series(w, z) for w in im_w), start=pd.Series(0.0, index=piv.index))
        e = float(ex[ex > 0].quantile(0.995)) if (ex > 0).sum() > 100 else None
        i = float(im[im > 0].quantile(0.995)) if (im > 0).sum() > 100 else None
        if e is None and i is None:
            continue
        names = {f"{a}>{b}" for (a, b) in NTC
                 if (a == z and b in ex_w) or (b == z and a in ex_w)
                 or (b == z and a in im_w) or (a == z and b in im_w)}
        out[z] = {"export": e, "import": i, "borders": sorted(names),
                  "export_legs": sorted(ex_w), "import_legs": sorted(im_w)}
    return out


def slice_ntc(ntc: dict, border, index) -> tuple:
    """(fwd, bwd) for one border over `index` — arrays where an hourly series exists, scalars otherwise.

    `highs_solver._build` runs each through `_as_time_array`, so a scalar broadcasts and an array is used
    as-is; no LP change is needed to carry hourly capacity."""
    v = ntc.get(border) or ntc.get((border[1], border[0])) or (0.0, 0.0)
    out = []
    for x in v:
        if hasattr(x, "reindex"):
            s = x.reindex(index)
            out.append(s.ffill().bfill().to_numpy(float) if s.notna().any() else 0.0)
        else:
            out.append(float(x))
    return tuple(out)


def modelled_zones(config) -> list[str]:
    """Zones that get their own balance row in the LP (i.e. everything but `BORDER_ONLY_ZONES`).

    Replaces four separate hardcoded `[z for z in config.all_zones if z != "GB"]` comprehensions that had
    drifted across backtest, projection, markup and blocks — one of them would eventually have been
    missed when GB was promoted."""
    return [z for z in config.all_zones if z not in BORDER_ONLY_ZONES]

# Zones dont les frontières sont **ré-allouées vers leurs proportions physiques, à total inchangé**.
#
# La NTC dérivée prend le p99.5 du flux *réalisé* : elle mesure l'usage, pas la capacité. Pour une zone dont
# les imports se répartissent sur plusieurs frontières dont aucune ne sature, chaque frontière est sous-lue
# individuellement — mais leur **somme** reste juste, car c'est le total simultané qui est physiquement
# contraint. Le cas type est DE→CH, lu sous son import réel (~3 600 MW observés en p99.5) parce que ce flux
# ne culmine pas quand les autres frontières suisses culminent. Depuis le split de DE_REST, CH a **quatre**
# frontières d'import ré-allouées (FR, DE-LU, IT-North et la nouvelle CH↔AT_SI) — la ré-allocation les couvre
# toutes.
#
# On corrige donc la **répartition** sans toucher au **total** : chaque frontière est portée à sa capacité
# physique (table `NTC`), puis l'ensemble est renormalisé pour retrouver le total dérivé. Plancher sans
# renormaliser a été essayé et rejeté — cela gonflait l'import simultané de CH à 9 144 MW (+61 % au-dessus
# du p99.5 observé) et son export à 11 200 MW, faisant d'elle un nœud de transit non physique qui noyait
# l'Italie (IT_NORTH −1,4 → −18 % de baseload).
_NTC_FLOOR_ZONES = frozenset({"CH"})


def _apply_ntc_floor(ntc: dict) -> dict:
    """Ré-alloue les frontières de `_NTC_FLOOR_ZONES` vers leurs proportions physiques, **à total inchangé**.

    Deux directions par zone, traitées séparément (import vers la zone, export depuis la zone) : chacune est
    portée à la capacité physique de la table `NTC`, puis mise à l'échelle pour que sa somme égale celle de
    la NTC dérivée. Le facteur de coïncidence de `flow_derived_ntc` — qui borne le transit *simultané* — est
    ainsi préservé, alors qu'un simple plancher le contournait.
    """
    out = dict(ntc)
    for z in _NTC_FLOOR_ZONES:
        borders = [b for b in NTC if z in b and b in out]
        if not borders:
            continue
        # index (border, position) des directions entrantes puis sortantes pour la zone z
        imp = [(b, 0 if b[1] == z else 1) for b in borders]
        exp = [(b, 0 if b[0] == z else 1) for b in borders]
        for legs in (imp, exp):
            derived = sum(out[b][i] for b, i in legs)
            phys = sum(NTC[b][i] for b, i in legs)
            if derived <= 0 or phys <= 0:
                continue
            k = derived / phys                       # renormalisation : conserve le total simultané
            for b, i in legs:
                pair = list(out[b])
                pair[i] = NTC[b][i] * k
                out[b] = tuple(pair)
    return out


def flow_derived_ntc(config: Config, year: int, coincident: bool = True) -> dict:
    """Effective NTC per border/direction from realized physical flow.

    Base = p99.5 of realized flow per border/direction (≈ usable transfer capability; shapes historical
    spreads). But per-border p99.5s peak at *different* times, so their sum overstates a zone's achievable
    **simultaneous** export — and that phantom headroom is exactly what let the model clear surplus that in
    reality priced negative (2019 DE: model ~16 GW export headroom vs ~14 GW simultaneous observed → far
    too few negative hours). With `coincident=True` each zone's export directions are scaled by a
    **coincidence factor** = (p99.5 of its *total simultaneous* export) / (Σ of its per-border p99.5s),
    so region-wide-surplus hours congest together as they do physically. Falls back to the flat default
    where a border's flow history is missing.
    """
    import sqlite3
    con = sqlite3.connect(config.resolve(config.section("data")["sqlite_path"]))
    try:
        df = pd.read_sql("SELECT ts_utc, series_key, value FROM entsoe_flows "
                         "WHERE ts_utc >= ? AND ts_utc < ?",
                         con, params=(f"{year}-01-01", f"{year + 1}-01-01"))
    finally:
        con.close()
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    piv = df.pivot_table(index="ts", columns="series_key", values="value").fillna(0.0)

    def flow_series(x, y):
        """Hourly x→y flow, summing constituent borders (virtual zones have no series of their own)."""
        cols = [f"{i}>{j}" for i in constituents(x) for j in constituents(y) if f"{i}>{j}" in piv.columns]
        return piv[cols].sum(axis=1) if cols else pd.Series(0.0, index=piv.index)

    base = {}
    for (a, b), (dab, dba) in NTC.items():
        va, vb = flow_series(a, b), flow_series(b, a)
        base[(a, b)] = (float(va[va > 0].quantile(0.995)) if (va > 0).sum() > 100 else float(dab),
                        float(vb[vb > 0].quantile(0.995)) if (vb > 0).sum() > 100 else float(dba))
    if not coincident:
        return _apply_ntc_floor(base)

    # per-zone export coincidence: cap Σ(border p99.5) at the p99.5 of *total simultaneous* export
    zones = {z for bd in NTC for z in bd}
    factor = {}
    for z in zones:
        neigh = [b for (a, b) in NTC if a == z] + [a for (a, b) in NTC if b == z]
        sum_cap = sum(base[(z, w)][0] if (z, w) in base else base[(w, z)][1] for w in neigh)
        total = sum((flow_series(z, w) for w in neigh), start=pd.Series(0.0, index=piv.index))
        sim = float(total[total > 0].quantile(0.995)) if (total > 0).sum() > 100 else sum_cap
        factor[z] = min(1.0, sim / sum_cap) if sum_cap > 0 else 1.0

    out = {}
    for (a, b), (ab, ba) in base.items():
        out[(a, b)] = (ab * factor.get(a, 1.0), ba * factor.get(b, 1.0))
    return _apply_ntc_floor(out)


_REGIME_NTC_MEMO: dict = {}      # (db_path, year) → result; the measurement is deterministic per year
#                                  and its full-year loads cost minutes — repeated flex backtests in one
#                                  process (tests, probes) must not re-pay it.


def regime_ntc(config: Config, year: int, base: dict) -> dict:
    """Surplus-regime border caps: {"mask": {zone: hourly bool}, "caps": {(a,b): (reg_ab, reg_ba)}}.

    The static caps over-couple exactly on surplus hours: measured 2024+2025, observed flows on the
    exporter's low-residual-load hours run at a median 0-60 % of the p99.5 cap with an at-cap share of
    0-6 %, while prices decouple across the border on 34-100 % of boundary hours — under flow-based
    market coupling the exchange domain SHRINKS when RES is high (loop flows), the exact anti-correlation
    a static cap misses. Result: the model's boundary hours are regionally synchronized (~2000 h in every
    zone, ±5 %) where reality's spread 435-1458 (`scratchpad/border_regime_caps.py`).

    Regime = the exporter's residual load (demand − must-take RES, raw data — deliberately NOT the
    flex-uplifted window inputs, so the regime definition cannot drift with model changes) below its
    year p20. Cap on regime hours = p95 of the observed flow on those hours (revealed capability: with
    prices decoupled and flows below cap, the binding constraint IS the capability). Fundamentals-only
    conditioning — projection-valid, no price leakage. Falls back to the static cap where the flow or
    residual history is thin — which no longer includes GB, now that Elexon supplies its load."""
    from ..io.entsoe_hist import load_demand_hist, load_generation_hist
    import sqlite3
    memo_key = (config.resolve(config.section("data")["sqlite_path"]), int(year))
    if memo_key in _REGIME_NTC_MEMO:
        return _REGIME_NTC_MEMO[memo_key]
    mt_techs = ("solar", "wind_onshore", "wind_offshore", "ror")
    con = sqlite3.connect(config.resolve(config.section("data")["sqlite_path"]))
    try:
        df = pd.read_sql("SELECT ts_utc, series_key, value FROM entsoe_flows "
                         "WHERE ts_utc >= ? AND ts_utc < ?",
                         con, params=(f"{year}-01-01", f"{year + 1}-01-01"))
    finally:
        con.close()
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    piv = df.pivot_table(index="ts", columns="series_key", values="value").fillna(0.0)
    piv = piv.resample("1h").mean()

    def flow_series(x, y):
        cols = [f"{i}>{j}" for i in constituents(x) for j in constituents(y) if f"{i}>{j}" in piv.columns]
        return piv[cols].sum(axis=1) if cols else pd.Series(dtype=float)

    zones = {z for bd in NTC for z in bd}
    mask = {}
    for z in zones:
        try:
            cons = constituents(z)
            dem = load_demand_hist(config, year, zones=cons)
            g = load_generation_hist(config, year, zones=cons)
        except Exception:                             # noqa: BLE001 — no data (GB) → no regime for z
            continue
        if dem.empty or g.empty:
            continue
        d = dem.groupby("timestamp_utc")["load_mw"].sum()
        mt = (g[g["tech"].isin(mt_techs)].groupby("timestamp_utc")["gen_mw"].sum())
        resid = (d - mt.reindex(d.index).fillna(0.0)).dropna()
        if len(resid) < 1000:
            continue
        mask[z] = resid < resid.quantile(0.20)
    caps = {}
    for (a, b), (ab, ba) in base.items():
        reg_ab, reg_ba = ab, ba
        for direction, (x, y, cap_static) in enumerate((( a, b, ab), (b, a, ba))):
            m = mask.get(x)
            f = flow_series(x, y)
            if m is None or f.empty:
                continue
            fr = f.reindex(m.index[m]).dropna()
            if len(fr) >= 100:
                reg = min(float(fr.quantile(0.95)), cap_static)
                if direction == 0:
                    reg_ab = reg
                else:
                    reg_ba = reg
        caps[(a, b)] = (reg_ab, reg_ba)
    out = {"mask": mask, "caps": caps}
    _REGIME_NTC_MEMO[memo_key] = out
    return out


def regime_cap_arrays(borders: list, ntc: dict, rn: dict, T) -> dict:
    """Per-window NTC arrays: the static cap everywhere, the regime cap on the EXPORTER's surplus
    hours (per direction, per hour — the LP broadcasts scalars or arrays via `_as_time_array`)."""
    out = {}
    for (a, b) in borders:
        ab, ba = ntc[(a, b)]
        reg_ab, reg_ba = rn["caps"].get((a, b), (ab, ba))
        ma, mb = rn["mask"].get(a), rn["mask"].get(b)
        arr_ab = np.where(ma.reindex(T).fillna(False).to_numpy(), reg_ab, ab) if ma is not None else ab
        arr_ba = np.where(mb.reindex(T).fillna(False).to_numpy(), reg_ba, ba) if mb is not None else ba
        out[(a, b)] = (arr_ab, arr_ba)
    return out


def _month_prices(cm: CommodityModel, ts: pd.Timestamp) -> dict:
    pm = cm.monthly_prices(ts.year, ts.year)
    row = pm[(pm["date"].dt.month == ts.month)]
    return {c: row[row["commodity"] == c]["price"].iloc[0] for c in ["gas", "co2", "coal", "oil"]}


def _fr_inputs(config, start, end, prices, nuc_avail_mult: float = 1.0) -> dict:
    h = load_fr_netload(config, str(start), str(end)).set_index("timestamp_utc")
    T = h.index
    stack = build_fr_stack(config)
    stack = stack[~stack["tech"].isin(_EXCLUDE_DISPATCH)].reset_index(drop=True)   # ROR/PSP handled elsewhere
    # the GB border-import tranches that used to be appended here are gone: GB is a modelled zone with a
    # real FR-GB border now, and keeping both would double its interconnector (see `windows.fr_stack_base`)
    stack = stack.assign(srmc_eur_mwh=srmc(stack, prices).to_numpy())
    # availability: nuclear rolling-max-of-output proxy; thermal 0.95; reservoir at capacity (budget-limited)
    nuc_cap = stack.loc[stack["tech"] == "nuclear", "capacity_mw"].sum()
    nuc_frac = np.clip(pd.Series(h["gen_nuclear_mw"].to_numpy()).rolling(72, 1).max().to_numpy()
                       / max(nuc_cap, 1) * 1.03 * nuc_avail_mult, 0, 1)
    fr = {"nuclear": nuc_frac, "gas": 0.95, "coal": 0.95, "oil": 0.95, "biomass": 0.85,
          "hydro_reservoir": 1.0, "import": 1.0}
    av = np.ones((len(stack), len(T)))
    for i, t in enumerate(stack["tech"]):
        av[i, :] = fr.get(t, 0.9)                              # scalar or per-hour array both broadcast
    avail = xr.DataArray(av, coords=[("unit", stack["unit_id"].to_numpy()), ("time", T)])
    budget = float(h["gen_hydro_reservoir_mw"].sum())                       # actual reservoir energy this window
    return {"stack": stack, "demand": h["demand_mw"].to_numpy(),
            "res_pot": h["musttake_res_mw"].to_numpy(), "avail": avail,
            "energy_caps": {"hydro_reservoir": budget}, "times": T}


def _neighbour_inputs(config, zone, start, end, year, prices, ref_times) -> dict:
    st = build_neighbour_stack(config, zone, year)
    st = st[~st["tech"].isin(_EXCLUDE_DISPATCH)].reset_index(drop=True)
    st = st.assign(srmc_eur_mwh=srmc(st, prices).to_numpy())
    nl = neighbour_netload(config, zone, year).set_index("timestamp_utc").reindex(ref_times)
    nl[["load_mw", "musttake_res_mw"]] = nl[["load_mw", "musttake_res_mw"]].interpolate().ffill().bfill()
    # reservoir weekly budget = actual reservoir generation in the window
    from ..io.entsoe_hist import load_generation_hist
    g = load_generation_hist(config, year, zones=constituents(zone))   # virtual zones sum their constituents
    if not g.empty and "tech" in g.columns:
        g = g[(g["tech"] == "hydro_reservoir")
            & (g["timestamp_utc"] >= ref_times[0]) & (g["timestamp_utc"] <= ref_times[-1])]
    else:
        g = g.iloc[0:0]
    caps = ({"hydro_reservoir": float(g["gen_mw"].sum())}
            if not g.empty and (st["tech"] == "hydro_reservoir").any() else {})
    return {"stack": st, "demand": nl["load_mw"].to_numpy(),
            "res_pot": nl["musttake_res_mw"].to_numpy(), "avail": None, "energy_caps": caps}


def assemble_window(config: Config, start, end, zones=None, price_mult=None,
                    nuc_avail_mult: float = 1.0) -> tuple:
    """→ (times, zones_data, borders, ntc) for the multi-zone LP over [start, end).

    `price_mult` scales commodity prices ({"gas":1.5} etc.) and `nuc_avail_mult` scales FR nuclear
    availability — used for the §8 projection-sensitivity checks.
    """
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    cm = CommodityModel.from_workbook(config.resolve(config.section("assumptions")["workbook"]))
    prices = _month_prices(cm, start)
    if price_mult:
        prices = {k: v * price_mult.get(k, 1.0) for k, v in prices.items()}
    zones = zones or modelled_zones(config)

    fr = _fr_inputs(config, start.date(), end.date(), prices, nuc_avail_mult=nuc_avail_mult)
    T = fr["times"]
    zd = {"FR": {k: v for k, v in fr.items() if k != "times"}}
    for z in zones:
        if z == "FR":
            continue
        try:
            zd[z] = _neighbour_inputs(config, z, start.date(), end.date(), start.year, prices, T)
        except (KeyError, ValueError):          # zone lacks data for this year (e.g. a cluster with no 2019
            continue                            # generation) → drop it, mirroring run_backtest's neighbour loop
    borders = [b for b in NTC if b[0] in zd and b[1] in zd]
    ntc = {b: NTC[b] for b in borders}
    return T, zd, borders, ntc
