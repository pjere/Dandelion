"""Switzerland's under-reported run-of-river — a FEED-COVERAGE defect, repaired at source in 2025.

The ENTSO-E Swiss run-of-river series reads 1.95 / 1.85 / 2.13 / 2.27 TWh in 2019/2022/2023/2024 and then
**14.50 TWh in 2025**. Swiss reservoir moves the other way over the same boundary (13.9 -> 9.9 TWh), so the
step is partly new filers and partly a reclassification between the two hydro categories. Either way it is
a reporting change, not water: `entsoe_installed_capacity` shows CH ROR flat across it, and the zone's own
declaration betrays the partial coverage before 2025 — 0.63 GW of declared nameplate against a metered
series that PEAKS at 0.98 GW, which is impossible.

WHY IT MATTERS. Run-of-river is must-take, uncurtailable, and peaks on snowmelt — and Swiss negative prices
ARE a snowmelt phenomenon: observed CH negative hours cluster in April-July, 09:00-14:00 (2024: Apr 50,
May 48, Jun 54, Jul 62). With five sixths of the fleet missing the zone has no surplus to price, which is
why the model prints **15 negative hours against 613 observed**, and why in the hours Switzerland really
did clear negative the model's median is **+22.35 EUR/MWh** — nowhere near its floor. CH is not floored,
it is empty.

THE ANCHOR IS THE SAME SERIES IN THE YEAR IT IS COMPLETE — not a national statistic, and not the zone's
balance residual. An earlier version of this module reconstructed from the residual (load - generation -
net imports, 2.10 GW mean on 2024) and it was WRONG for a reason worth recording: the residual collapses
from 18.5 TWh to 5.6 TWh across the same 2024/2025 boundary, so it was tracking reporting completeness,
not hydrology. It produced a 3x swing between adjacent years and a 4.99 GW peak, 36 % above anything the
Swiss fleet has ever delivered. The residual is kept below only as a cross-check.

The 2025 feed gives both the level and the envelope:

    year   TWh    mean GW   peak GW      normalised monthly shape, corr vs 2025
    2019   1.95      0.22      0.46      0.823
    2022   1.85      0.21      0.48      0.904
    2023   2.13      0.24      0.49      0.848
    2024   2.27      0.26      0.98      0.809
    2025  14.50      1.66      3.67      1.000   <- complete

THE SUBSET IS SHAPE-REPRESENTATIVE, which is the assumption the whole reconstruction rests on and is why
it is stated here rather than assumed: every partial year's normalised monthly profile correlates 0.81-0.90
with the complete year's, and all of them peak in June-July and trough in February-March. So the partial
filers are a scaled-down sample of the same fleet on the same freshet, and scaling them is legitimate;
their own year-to-year variation carries that year's hydrology.

    reconstructed = metered x k,  k = (2025 total) / (mean of the partial years) = 14.50 / 2.05 = 7.07

CAPPED AT THE 2025 ENVELOPE (p99 = 3.39 GW). A scale factor applied to a series whose own peak is erratic
— 2024's is 0.98 GW against 0.46-0.49 GW in the other partial years — can otherwise assert more capacity
than the fleet possesses. The cap is what the complete feed proves the fleet can deliver.

ADDED TO MUST-TAKE, NOT NETTED OFF LOAD, and the distinction is deliberate. `gb_embedded` nets Britain's
residual off demand precisely so it CANNOT price, because heat-led embedded CHP does not respond to price.
Swiss ROR is the opposite case: it is what spills in a Swiss surplus, it is curtailable at deep spill cost,
and the zone's own bid ladder (KEV 0.75 at -50, merchant 0.25 at 0.00) is what should set the price when it
does. Netting it off load would create the surplus and then hide it.

Opt out with `DISPATCH_CH_ROR=0`.
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

from ..config import Config
from ..framecache import FrameCache, db_key

#: (zone, tech) whose ENTSO-E series is under-reported before a coverage fix, with the year the feed
#: became complete. CH run-of-river is the only entry, and the sweep behind that claim is recorded in
#: `DECISIONS.md`: of 51 candidate steps across every zone and technology, this is the one that is large,
#: in a SCORED zone, and removes a whole must-take fleet. The rest are closures, reclassifications between
#: unscored Italian/Danish sub-zones, or ordinary year-to-year variation.
_COVERAGE_FIXED = {("CH", "hydro_ror"): 2025}

#: Quantile of the complete year used as the reconstruction's ceiling. Not the max: the complete feed's
#: own maximum is a single hour and a scale factor should not be licensed to reproduce it.
_ENVELOPE_Q = 0.99

_CACHE = FrameCache(maxsize=16)


def enabled() -> bool:
    """Read at each call site so an A/B arm can flip it without reimporting."""
    return os.environ.get("DISPATCH_CH_ROR", "1") not in ("0", "false", "False")


def _tech_series(config: Config, zones, year: int, tech: str) -> pd.Series | None:
    from .entsoe_hist import load_generation_hist
    try:
        g = load_generation_hist(config, year, zones=list(zones))
    except (KeyError, ValueError):
        return None
    if g.empty or "tech" not in g.columns:
        return None
    s = g[g["tech"] == tech].groupby("timestamp_utc")["gen_mw"].sum()
    return s if len(s) >= 1000 else None


def coverage_factor(config: Config, zone: str, tech: str, year: int) -> tuple[float, float] | None:
    """→ (k, envelope_mw) for an under-reported (zone, tech) series, or None if it needs no repair.

    `k` scales the metered series to the level the complete year reports; `envelope_mw` caps the result.
    Returns None for the complete years themselves, and for any zone/tech not in `_COVERAGE_FIXED`.
    """
    fixed = _COVERAGE_FIXED.get((zone, tech))
    if fixed is None or int(year) >= int(fixed):
        return None                                  # complete year — nothing to repair
    full = _tech_series(config, [zone], fixed, tech)
    if full is None or float(full.sum()) <= 0:
        return None
    partials = []
    for y in range(int(fixed) - 6, int(fixed)):
        s = _tech_series(config, [zone], y, tech)
        if s is not None and float(s.sum()) > 0:
            partials.append(float(s.mean()))
    if not partials:
        return None
    k = float(full.mean()) / float(np.mean(partials))
    return (k, float(full.quantile(_ENVELOPE_Q))) if k > 1.2 else None


def missing_mw(config: Config, zone: str, year: int) -> pd.Series | None:
    """→ hourly MW to ADD to `zone`'s must-take: the un-reported part of its run-of-river fleet."""
    tech = "hydro_ror"

    def build() -> pd.DataFrame | None:
        cf = coverage_factor(config, zone, tech, year)
        s = _tech_series(config, [zone], year, tech)
        if cf is None or s is None:
            return None
        k, envelope = cf
        scaled = (s * k).clip(upper=envelope)
        return pd.DataFrame({"missing_mw": (scaled - s).clip(lower=0.0)})

    df = _CACHE.get_or_build((db_key(config), "ch_ror", str(zone), int(year)), build)
    return None if df is None else df["missing_mw"]


def apply_to_netload(config, zone: str, year: int, df: pd.DataFrame) -> pd.DataFrame:
    """Add the un-reported run-of-river to `zone`'s must-take RES. No-op elsewhere or when disabled.

    `df` is indexed by timestamp_utc and carries `load_mw` / `musttake_res_mw`; the caller recomputes
    `netload_mw`. It goes to MUST-TAKE rather than off load so the zone's RES bid ladder can price the
    surplus it creates — see the module docstring.
    """
    if df.empty or not enabled() or (zone, "hydro_ror") not in _COVERAGE_FIXED:
        return df
    add = missing_mw(config, zone, year)
    if add is None:
        return df
    a = add.reindex(df.index).ffill().bfill().fillna(0.0).to_numpy()
    out = df.copy()
    out["ch_missing_ror_mw"] = a
    out["musttake_res_mw"] = out["musttake_res_mw"].to_numpy() + a
    return out
