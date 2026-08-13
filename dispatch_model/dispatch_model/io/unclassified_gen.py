"""Metered generation that belongs to NO dispatch class — `waste`, `geothermal`, `other_res`, `other`.

`PSR2TECH` maps sixteen ENTSO-E labels to model technologies. `neighbours.blocks` then splits those
technologies into `_DISPATCHABLE` (they bid) and `_MUSTTAKE` (they set net load). **Four of the sixteen
appear in neither list**, so until this module existed they were read out of the lake, mapped to a tech,
and then silently dropped — the generation was metered, loaded into memory, and discarded.

Measured cost on 2024, as a share of each zone's own load:

    NL  34.6 %      IT_NORTH 8.8 %   IT_SOUTH 8.2 %   PL_CZ 2.8 %   BE 2.7 %
    DK   2.3 %      DE_LU    1.9 %   AT_SI    1.8 %   GB    1.3 %   ES 1.2 %

The Netherlands is not a marginal case. 4.53 GW mean / 39.8 TWh of Dutch generation had no representation
at all, which is the single largest error in the model: NL's stack was asked to serve a net load its real
fleet never sees. The closing balance, 2024 hourly means:

    model asks NL dispatchable for  10.91 GW   (net load 10.44 + net exports 0.47)
    observed NL thermal              4.91 GW
              + Other                4.21
              + Waste                0.32
              + flat load residual   1.48
                                    10.92 GW   <- closes to 0.01 GW

THE FOUR ARE NOT ONE PHENOMENON, and treating them alike would be its own error. Measured per zone on
2024 — `day` is the 10-15 UTC mean, `night` the 22-05 mean, `corr` the correlation with that zone's own
day-ahead price:

    waste / geothermal / other_res    day/night 0.95-1.01, corr ~0   -> flat must-run, every zone
    NL `other`                        day/night 3.04, corr -0.444    -> SOLAR (see below)
    every other zone's `other`        day/night 0.98-1.39, corr +0.09..+0.47 -> price-following

`Waste` is waste-to-energy and `Geothermal` is Larderello: IT_CNOR's geothermal averages 599 MW against a
636 MW peak — flat to within 6 % — and waste-to-energy burns because the waste arrives, not because the
price cleared. Both are must-run by physics, in every zone, and are taken whole.

NL `Other` IS DUTCH SOLAR, and the evidence is not circumstantial:

    diurnal      2.0 GW at 03:00 -> 7.7 GW at 13:00 (day/night 3.04; no other zone exceeds 1.39)
    seasonal     peaks April-July, troughs October-December
    corr with NL's own `Solar` series                                    +0.871
    corr with price                                                      -0.444  (it CAUSES the lows)
    NL's `Solar` key itself reads 0.055 GW mean / 0.399 GW peak — against a fleet of ~26 GW

TenneT reports the decentralised fleet under `Other` and leaves `Solar` a near-empty stub, so a model that
reads `Solar` sees 0.4 GW of peak Dutch PV where ~12 GW exists. That is why NL carried 458 observed
negative hours against roughly zero modelled, and why the winter evenings — when the solar part is zero and
only the industrial floor remains — ran to VoLL: the missing 3.3 GW at 2024-01-17 16:00 against a measured
shortfall of 0.8 GW.

NETTING IS SOUND HERE, AND THAT WAS CHECKED RATHER THAN ASSUMED — it is exactly the trap `gb_embedded`
documents from the other side. If NL's load were already net of this generation, subtracting it again would
double-count. The 2024 balance says it is not:

    load 13.10 GW = generation 12.09 + net imports (-0.47) + residual 1.48
    residual   p10 1.45   p50 1.48   p90 1.50   |   day 1.47   night 1.48

A residual that is FLAT to ±0.03 GW across day and night carries no solar shape, so the load series is
gross and the generation is genuinely additional. (The flat 1.48 GW itself is a separate, unexplained
load-definition offset — station service, pumping, or a boundary difference. It is level, not shape, so it
biases NL's level slightly and is left for a later pass rather than fudged into this one.)

THE DECOMPOSITION IS EXACT. `other` is split into a floor and a remainder:

    mustrun[h] = min(floor[day(h)], other[h])   floor = that day's night minimum, 7-day rolling median
    variable[h] = other[h] - mustrun[h]         >= 0 by construction

so `mustrun + variable == other` in every hour, with no leakage in either direction. The floor is what the
fleet never drops below, which is a price-insensitive statement whatever the fuel is, so it is taken as
must-run in EVERY zone. The remainder is added to must-take RES only where the series is demonstrably
solar-shaped (`_SOLAR_DAY_NIGHT` = 2.0; NL scores 3.04, the runner-up 1.39 — the gap is wide enough that
the threshold is not doing delicate work).

WHAT IS DELIBERATELY LEFT OUT: the variable part of `other` in the zones where it is price-FOLLOWING
(IT_NORTH 12.0 TWh at corr +0.330, GB 3.3 TWh at +0.465, the Italian south). That generation is real and
it is dispatchable, so representing it needs a short-run marginal cost — and `other` is a residual label
with no fuel, so there is no SRMC to source. Netting it off load would assert it is must-run, which the
positive price correlation refutes; a guessed SRMC would be an invented supply curve in the two zones whose
levels are already the worst in the model. It stays out until it can be given a cost, and this paragraph
exists so the omission is found by measurement rather than rediscovered as a surprise.

The energy split, per zone, is printed by `scripts/audit_unclassified.py`.

WHAT SHIPPED, AND WHAT DID NOT. The two halves were measured separately on the multi-year gate and they
do not behave alike, so they ship on separate flags:

    arm                                    |mean err|   log_err   NL 2024 neg (obs 458)
    pre-fix baseline                            11.15     0.737                    951
    solar half only          <- SHIPPED         11.00     0.692                    379
    both halves                                 12.71     0.785                    742
    both, solar netted off load instead         13.88     0.849                   1829

The SOLAR half is a clear win and is on by default: it beats the baseline on both pooled metrics while
leaving every other zone within 0.5 EUR/MWh (FR -4.3->-4.2, DE_LU -23.9->-23.8, CH +7.3->+7.2), because
it is a NL-only correction. It does not add supply — it REPLACES the synthetic `btm_solar` reconstruction
(see below) with the metered series.

The MUST-RUN half is off by default (`DISPATCH_UNCLASSIFIED_MUSTRUN`). It costs 1.7 EUR/MWh of pooled mean
error, because it lowers every zone ~4 EUR/MWh — helping the three the model prices too dear and hurting
the five it already prices too cheap. The generation is real, so this is not a correctness verdict: it is
that adding correct supply to a model with a pre-existing cheap bias moves it further from observation.
The bias has to be found first. `DECISIONS.md` records the leading candidates.

IT SUPERSEDES `flexibility.res_potential.btm_solar`, and that is the point rather than a side effect.
The model already reconstructed Dutch behind-the-meter PV, on the stated premise that the fleet is
"invisible on BOTH sides of the ENTSO-E balance — not in generation (behind-the-meter)". That premise is
false: the "0.5 TWh/yr metered" it cites is the `Solar` key alone (0.055 GW x 8784 h = 0.49 TWh), and the
fleet sits in `Other`. The synthetic estimator normalises the 0.4 GW stub by its own p99.9 and rides
27.6 GW of nameplate on that shape:

                        energy      peak    peak/nameplate
    btm_solar          28.4 TWh   22.28 GW      0.80
    metered `Other`    22.3 TWh   13.04 GW      0.47
    actual (IRENA/CBS) ~21-23 TWh

corr(btm, metered) = +0.913 — the same fleet twice. The energies nearly agree; the PEAK is what was wrong,
and a 0.80 peak factor across 28 GW of mixed roof pitch is unreachable. That excess peak was NL's phantom
midday surplus: 727 model-only negative hours in 2024, every one of them 05-15 UTC, March-September.
Running both paths gives 50.6 TWh at a 35.3 GW peak against a real ~21 TWh, which is what an earlier
version of this module did and why it read as "correct but harmful" before the double-count was found.

The `btm_solar` call sites in `rolling/backtest.py` and `rolling/projection.py` are gated on
`solar_enabled()`, so disabling this half restores them rather than leaving NL with no solar at all.

NL IS NOT FIXED, and the residual is not a solar problem. NL still runs +15.5 EUR/MWh too dear on 2024
while producing 379 negative hours — both tails wrong in opposite directions, which is what an ISOLATED
zone looks like. `rolling/assemble.hourly_ntc` caps NL>DE_LU at 1081 MW flat, on a border whose observed
flow reaches 5171 MW (2024) / 6231 MW (2025) and exceeds the cap in 2839 and 4347 hours respectively. See
`DECISIONS.md`; that is the next chantier, and it is cross-zonal (FR>CH, DE_LU>CH and FR>IT_NORTH are
throttled the same way).

The energy split, per zone, is printed by `scripts/audit_unclassified.py`.
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

from ..config import Config
from ..framecache import FrameCache, db_key

#: Metered as generation, dropped by both `_DISPATCHABLE` and `_MUSTTAKE`.
UNCLASSIFIED = ("waste", "geothermal", "other_res", "other")

#: Taken whole as must-run: measured day/night ratio 0.95-1.01 in every zone that reports them.
_FLAT_MUSTRUN = ("waste", "geothermal", "other_res")

#: `other` whose 10-15 UTC mean exceeds its 22-05 mean by this factor is solar, and its variable part is
#: credited to must-take RES. NL scores 3.04; the next zone down is GB at 1.39.
_SOLAR_DAY_NIGHT = 2.0

#: UTC hours used to measure the must-run floor of `other`. In CET/CEST these are 00-04 / 01-05 local —
#: after dusk and before dawn all year, so a solar-carrying series reads its non-solar component here.
_NIGHT_HOURS = (23, 0, 1, 2, 3)

_CACHE = FrameCache(maxsize=32)


def enabled() -> bool:
    """Env flag, read at each call site so tests and A/B arms can flip it without reimporting.

    DEFAULT ON, with the must-run half OFF (`mustrun_enabled`) — the configuration the multi-year gate
    measured best. See "What shipped, and what did not" in the module docstring.
    """
    return os.environ.get("DISPATCH_UNCLASSIFIED_GEN", "1") not in ("0", "false", "False")


def _on(name: str) -> bool:
    return os.environ.get(name, "1") not in ("0", "false", "False")


def mustrun_enabled() -> bool:
    """The flat must-run half: `waste`/`geothermal`/`other_res` + the `other` night floor, netted off load.

    Separable from the solar half because the two are independent corrections that happened to arrive in
    one change, and they behave differently. This half is CROSS-ZONAL (DE_LU 9.0 TWh, IT_NORTH 9.9, NL
    17.5, ES 2.7, BE 2.1, FR 1.8) and it lowers the whole system by ~4 EUR/MWh — which helps the zones
    priced too dear and hurts the five already priced too cheap.

    DEFAULT OFF. Measured on the multi-year gate it costs 1.7 EUR/MWh of pooled mean error (11.00 ->
    12.71) and 0.09 of log_err (0.692 -> 0.785) against the solar half alone. The generation is real, so
    this is not a correctness verdict — it is that adding correct supply to a model already biased cheap
    in five of eight zones moves it further from the observations. Re-enable with
    `DISPATCH_UNCLASSIFIED_MUSTRUN=1` once that bias is understood (see `DECISIONS.md`).
    """
    return os.environ.get("DISPATCH_UNCLASSIFIED_MUSTRUN", "0") not in ("0", "false", "False")


def solar_enabled() -> bool:
    """The solar half: `other`'s variable part credited to must-take where the shape is solar (NL only).

    This one REPLACES rather than adds — see the superseded `btm_solar` blocks in `rolling/backtest.py`
    and `rolling/projection.py`. Disable with `DISPATCH_UNCLASSIFIED_SOLAR=0`.
    """
    return _on("DISPATCH_UNCLASSIFIED_SOLAR")


def _hourly(config: Config, zones, year: int, tech) -> pd.Series | None:
    """Hourly MW summed over `zones` for `tech` (str or tuple), or None if nothing is reported."""
    from .entsoe_hist import load_generation_hist
    g = load_generation_hist(config, year, zones=list(zones))
    if g.empty:
        return None
    keep = (tech,) if isinstance(tech, str) else tuple(tech)
    s = g[g["tech"].isin(keep)].groupby("timestamp_utc")["gen_mw"].sum()
    return s if len(s) else None


def _split_other(s: pd.Series) -> tuple[pd.Series, pd.Series, float]:
    """`other` -> (must-run floor part, variable part, day/night ratio). The two parts sum to `s` exactly.

    THE FLOOR TRACKS THE NIGHTLY LEVEL DAY BY DAY, and the first version of this function did not — it
    used a per-month p10 over the night hours, which by construction sits *below* the typical night. The
    residue showed up exactly where it must: 0.9 GW of "solar" at midnight, and a Dutch PV total of
    26.3 TWh against the ~21 TWh IRENA reports. A floor must sit AT the night level, not under its tenth
    percentile, because solar at night is zero rather than small.

    So the floor is each day's minimum over `_NIGHT_HOURS`, smoothed by a centred 7-day rolling median.
    The rolling median is what makes it robust: the raw series dips to 0.06 GW on metering dropouts, and
    an unsmoothed daily minimum would hand those days' entire baseload to the solar bucket.

    Clipped hour-by-hour to the actual value, so the floor can never net off more than was metered and the
    two parts sum back to `s` in every hour.
    """
    idx = pd.DatetimeIndex(s.index)
    night = s[idx.hour.isin(_NIGHT_HOURS)]
    if len(night):
        daily = night.groupby(pd.DatetimeIndex(night.index).normalize()).min()
        daily = daily.rolling(7, center=True, min_periods=1).median()
        lvl = daily.reindex(idx.normalize()).ffill().bfill()
        lvl = pd.Series(lvl.to_numpy(), index=s.index)
    else:
        lvl = pd.Series(0.0, index=s.index)
    mustrun = np.minimum(lvl, s).clip(lower=0.0)
    day = float(s[(idx.hour >= 10) & (idx.hour <= 15)].mean())
    nig = float(s[(idx.hour >= 22) | (idx.hour <= 5)].mean())
    return mustrun, (s - mustrun).clip(lower=0.0), (day / nig if nig > 1e-9 else 0.0)


def components(config: Config, zones, year: int) -> pd.DataFrame | None:
    """→ hourly [mustrun_mw, res_mw] for the unclassified techs of `zones`, or None if there are none.

    `mustrun_mw` is netted off load (it grows with demand in projection, which is the right driver for
    waste / geothermal / industrial baseload). `res_mw` is credited to must-take (it grows with the RES
    trajectory, which is the right driver for the Dutch PV hiding in `other`).
    """
    zl = tuple(sorted(zones))

    def build() -> pd.DataFrame | None:
        parts: list[pd.Series] = []
        flat = _hourly(config, zl, year, _FLAT_MUSTRUN)
        if flat is not None:
            parts.append(flat.rename("flat"))
        res = None
        oth = _hourly(config, zl, year, "other")
        if oth is not None:
            mustrun, variable, ratio = _split_other(oth)
            parts.append(mustrun.rename("other_floor"))
            if ratio >= _SOLAR_DAY_NIGHT:
                res = variable                  # solar-shaped -> must-take RES (NL)
            # else: price-following, no SRMC to price it with -> dropped, see the docstring
        if not parts and res is None:
            return None                         # non-frame: passes through `get_or_build` uncached
        idx = parts[0].index if parts else res.index
        out = pd.DataFrame(index=idx)
        out["mustrun_mw"] = (sum(p.reindex(idx).fillna(0.0) for p in parts) if parts
                             else pd.Series(0.0, index=idx))
        out["res_mw"] = res.reindex(idx).fillna(0.0) if res is not None else 0.0
        return out

    return _CACHE.get_or_build((db_key(config), "unclassified", zl, int(year)), build)


def apply_to_netload(config, zone: str, year: int, df: pd.DataFrame) -> pd.DataFrame:
    """Fold a zone's unclassified generation into its net load. No-op when the flag is off or none exists.

    `df` is indexed by timestamp_utc and carries `load_mw` / `musttake_res_mw`; the caller recomputes
    `netload_mw`. Load is floored at zero for the same reason `gb_embedded` floors it — this subtracts
    measured supply, and a negative demand would be an estimator artefact rather than a real export.
    """
    if df.empty or not enabled() or zone == "GB":
        # GB is excluded BY CONSTRUCTION, not by exception. `gb_embedded` nets the measured residual
        # between Britain's load and its metered generation, and a residual absorbs *everything* not
        # otherwise represented — including these four techs. Applying both corrections would subtract
        # GB's `other`/`waste` floor twice, once explicitly and once inside the 5851 MW residual.
        return df
    from ..neighbours.blocks import constituents
    comp = components(config, constituents(zone), year)
    if comp is None:
        return df
    c = comp.reindex(df.index).fillna(0.0)
    if not mustrun_enabled():
        c["mustrun_mw"] = 0.0
    if not solar_enabled():
        c["res_mw"] = 0.0
    out = df.copy()
    out["unclassified_mustrun_mw"] = c["mustrun_mw"].to_numpy()
    out["unclassified_res_mw"] = c["res_mw"].to_numpy()
    # Must-run comes off LOAD; the solar part goes to MUST-TAKE. Net load is identical either way
    # (`load - mustrun - solar - musttake` regrouped), so this choice looks cosmetic and is not: what it
    # actually decides is whether the solar is CURTAILABLE.
    #
    # Netting it off load instead was tried and measured (v2): as inflexible negative demand it cannot be
    # curtailed, so NL's surplus hours ran past the RES bid floor all the way to the -500 EUR/MWh price
    # floor — **997 hours of 2024 at -500** — and NL's pooled mean error went -19.8 -> -60.6. Every other
    # zone was bit-identical, which is the tell: the thermal dispatch never moved, only NL's own dual.
    # As must-take the same energy is curtailable and the price stops at the bid floor (-20), which is
    # what a real system does with surplus PV.
    #
    # This REFINES the `gb_embedded` decision rather than contradicting it. That module nets embedded
    # generation off demand to avoid manufacturing negative prices GB does not have; the lesson here is
    # that the manoeuvre is only safe for genuinely inflexible generation. Applied to a 13 GW-peak solar
    # fleet in a zone that does price negative, it does not suppress the low tail — it detonates it.
    out["load_mw"] = np.maximum(out["load_mw"].to_numpy() - c["mustrun_mw"].to_numpy(), 0.0)
    out["musttake_res_mw"] = out["musttake_res_mw"].to_numpy() + c["res_mw"].to_numpy()
    return out
