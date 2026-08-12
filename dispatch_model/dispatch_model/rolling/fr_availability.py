"""FR nuclear + reservoir availability for PROJECTION, from the step-v availability_model lake (#160).

The backtest feeds `fr_window(nuc_unavail_daily=…)` with the true REMIT nuclear outage (#78); the projection
used to pass nothing and fell back to the *reference-year* rolling-max-of-output proxy — 2030's nuclear
availability assumed to look like 2019's. This wires the availability_model's PROJECTED output instead:

  * `availability_by_tech`  [draw, day, technology, available_mw] → daily nuclear availability FRACTION
    (available ÷ that draw-year's fleet peak). A fraction, not an absolute — so it decouples the *outage
    pattern* (maintenance schedule + forced outages the model projects) from the *capacity level*, which the
    dispatch sets from its own TYNDP trajectory (`nuc_cap`). Applied as outage MW = (1 − frac)·nuc_cap.
  * `reservoir_budget`      [week_start, avail_energy_gwh] → the weekly FR reservoir energy budget (MWh),
    replacing the window's reference-year reservoir generation as the `hydro_reservoir` energy cap.

Both are mapped from the target year onto the reference-year window calendar by position (day-of-year /
ISO-week), the same alignment the weather shapes use. Per-draw for nuclear (the availability model's Monte-
Carlo draw d, wrapped modulo its draw count); the reservoir budget is a single deterministic series.
Returns None when the lake is absent → the projection keeps its reference-year proxy (graceful).
"""
from __future__ import annotations

import pandas as pd


def load_fr_availability(config) -> dict | None:  # noqa: ARG001 (config kept for signature symmetry / future paths)
    """Load the step-v FR nuclear availability fraction (per draw) + weekly reservoir budget, or None.

    → {"nuc_frac": Series[(draw, year, doy) → frac], "nuc_draws": int,
       "reservoir": {(year, iso_week): budget_mwh}}."""
    from powersim_core import lake
    try:
        bt = lake.read_table("availability", "availability_by_tech")   # [draw, day, technology, available_mw]
    except (FileNotFoundError, KeyError, ValueError, OSError):
        return None
    nuc = bt[bt["technology"] == "nuclear"].copy()
    if nuc.empty:
        return None
    nuc["day"] = pd.to_datetime(nuc["day"])
    nuc["year"] = nuc["day"].dt.year
    nuc["doy"] = nuc["day"].dt.dayofyear
    # fleet peak per (draw, year) = the installed baseline the fraction is measured against (captures a fleet
    # that shrinks over the horizon as reactors retire — the fraction stays a pure availability signal).
    inst = nuc.groupby(["draw", "year"])["available_mw"].transform("max")
    nuc["frac"] = (nuc["available_mw"] / inst.where(inst > 0, 1.0)).clip(0.0, 1.0)
    frac = nuc.set_index(["draw", "year", "doy"])["frac"].sort_index()
    out: dict = {"nuc_frac": frac, "nuc_draws": int(nuc["draw"].nunique()), "reservoir": {}}
    try:
        rb = lake.read_table("availability", "reservoir_budget")       # [week_start, avail_energy_gwh]
        rb = rb.copy()
        rb["week_start"] = pd.to_datetime(rb["week_start"])
        yr = rb["week_start"].dt.year
        wk = rb["week_start"].dt.isocalendar().week.astype(int)
        out["reservoir"] = {(int(y), int(w)): float(v) * 1000.0        # GWh → MWh
                            for y, w, v in zip(yr, wk, rb["avail_energy_gwh"])}
    except (FileNotFoundError, KeyError, ValueError, OSError):
        out["reservoir"] = {}
    return out


def nuclear_unavail_daily(fr_avail: dict, target_year: int, ref_year: int, draw: int,
                          nuc_cap: float) -> dict | None:
    """{reference-year date → nuclear outage MW at `nuc_cap`} for `fr_window`, from the step-v availability
    fraction of (`draw` mod n_draws, `target_year`), mapped target→reference by day-of-year. None if absent."""
    frac = fr_avail.get("nuc_frac")
    nd = fr_avail.get("nuc_draws") or 0
    if frac is None or nd == 0 or nuc_cap <= 0:
        return None
    try:
        yr = frac.loc[(draw % nd, target_year)]        # Series indexed by day-of-year
    except KeyError:
        return None
    if yr.empty:
        return None
    doy_max = int(yr.index.max())
    ref_days = pd.date_range(f"{ref_year}-01-01", f"{ref_year + 1}-01-01", freq="D", tz="UTC")[:-1]
    out = {}
    for dt in ref_days:
        d = int(dt.dayofyear)
        f = yr.get(d)
        if f is None:                                   # leap-day / gap → clamp to the last available doy
            f = yr.get(min(d, doy_max), 1.0)
        out[dt.date()] = float((1.0 - float(f)) * nuc_cap)
    return out


def reservoir_budget_mwh(fr_avail: dict, target_year: int, window_start) -> float | None:
    """Step-v FR reservoir energy budget (MWh) for the window whose TARGET-year start is `window_start`
    (ISO-week keyed). None where absent → the window keeps its reference-year reservoir energy."""
    res = fr_avail.get("reservoir") or {}
    if not res:
        return None
    wk = int(pd.Timestamp(window_start).isocalendar().week)
    return res.get((target_year, wk))
