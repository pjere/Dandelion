"""Regime trajectories (FLEX-F0) — economically/regulatorily contingent, year-indexed workbook sheets.

Unlike the reactor *physics* (``reactor_class``), these evolve over the 2026–2046 horizon and are therefore
**workbook trajectories**, never frozen constants (spec §1.3, §7). Each loader reads its ``dispatch_*`` tab
via the standard ScenarioStore accessor and falls back to a documented default when the tab is absent — so
the module runs before the workbook is populated (same graceful pattern as ``tyndp.load_tyndp`` /
``neighbours.cohort``). Values below are **seeds**; the free ones (``c_mod``, ``c_start``) are fitted in
FLEX-F7 (§9). All sheets are long-schema ``[..., year, value]`` and interpolate linearly (clamped) in year.

Sheets:
  dispatch_flex_costs   [variable, year, value]  variable ∈ {c_mod, c_start_<class>}  (€/MWh, €/MW)
  dispatch_minstab      [zone, year, value]       grid-stability nuclear must-run floor P_minstab (MW)
  dispatch_reserves     [variable, year, value]   {r_up_req, r_down_req} fleet reserve requirement (MW)
  dispatch_oa_ladder    [variable, year, value]   {cr_bid, oa_bid, market_floor, market_cap} (€/MWh)
"""
from __future__ import annotations

import numpy as np

# ---- documented defaults (used when a workbook tab is absent) -------------------------------------------
# c_start is a CALIBRATION parameter (€/MW on the recommitment variable su); seeded so a weekend shutdown vs
# several hours of negative prices is a live arbitrage (F7 tunes it). c_mod (€/MWh on deep-mod depth d) is
# seeded to land fleet modulated energy at the observed ~30–33 TWh/yr; also F7.
_DEFAULT_FLEX_COSTS = {
    "c_mod": 8.0,
    "c_start_900": 300.0, "c_start_1300": 320.0, "c_start_N4": 340.0,
    "c_start_EPR": 300.0, "c_start_EPR2": 260.0,
    "c_start_gas": 30.0, "c_start_coal": 80.0, "c_start_lignite": 90.0, "c_start_oil": 20.0,
}
_DEFAULT_MINSTAB_MW = 0.0            # no explicit grid-stability floor pre-2026 (CRE mechanism is 2026+)
_DEFAULT_RESERVES = {"r_up_req": 1500.0, "r_down_req": 1000.0}   # FR fleet-level seed (MW)
_DEFAULT_OA_LADDER = {"cr_bid": -1.0, "oa_bid": -500.0, "market_floor": -500.0, "market_cap": 4000.0}


def _long(workbook, sheet: str):
    from powersim_core.scenario import load_sheet
    try:
        return load_sheet(workbook, "dispatch", sheet)
    except (ValueError, KeyError, FileNotFoundError, OSError):
        return None


def _interp(series: dict, year: int, default: float) -> float:
    if not series:
        return float(default)
    ys = np.array(sorted(series))
    vs = np.array([series[y] for y in ys], float)
    return float(np.interp(year, ys, vs))       # clamps flat outside the anchor range


def _by_var(df) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in df.itertuples():
        out.setdefault(str(r.variable), {})[int(r.year)] = float(r.value)
    return out


def load_flex_costs(workbook, year: int) -> dict[str, float]:
    """{c_mod, c_start_<class/tech>} at `year` (€/MWh, €/MW). Workbook `dispatch_flex_costs` overrides the
    seed defaults per variable; absent variables keep their default."""
    df = _long(workbook, "flex_costs")
    series = _by_var(df) if df is not None else {}
    return {k: _interp(series.get(k, {}), year, dflt) for k, dflt in _DEFAULT_FLEX_COSTS.items()}


def minstab_mw(workbook, zone: str, year: int) -> float:
    """Grid-stability nuclear must-run floor for `zone`/`year` (MW, C7). 0 where absent."""
    df = _long(workbook, "minstab")
    if df is None:
        return _DEFAULT_MINSTAB_MW
    series = {}
    for r in df.itertuples():
        if str(r.zone) == zone:
            series[int(r.year)] = float(r.value)
    return _interp(series, year, _DEFAULT_MINSTAB_MW)


def load_reserves(workbook, year: int) -> dict[str, float]:
    """{r_up_req, r_down_req} fleet reserve requirement at `year` (MW, C6)."""
    df = _long(workbook, "reserves")
    series = _by_var(df) if df is not None else {}
    return {k: _interp(series.get(k, {}), year, dflt) for k, dflt in _DEFAULT_RESERVES.items()}


def load_oa_ladder(workbook, year: int, price_floor: float = -500.0, price_cap: float = 4000.0) -> dict[str, float]:
    """Downward-ladder bid levels at `year` (€/MWh, §6): `cr_bid` (CR/merchant curtailment ≈0…−5),
    `oa_bid` (legacy obligation-d'achat, at the market floor), plus the `market_floor`/`market_cap` truncating
    both tails. Config's scarcity floor/cap seed the defaults; the workbook `dispatch_oa_ladder` overrides."""
    dflt = dict(_DEFAULT_OA_LADDER, oa_bid=float(price_floor), market_floor=float(price_floor),
                market_cap=float(price_cap))
    df = _long(workbook, "oa_ladder")
    series = _by_var(df) if df is not None else {}
    return {k: _interp(series.get(k, {}), year, d) for k, d in dflt.items()}


# FR RES scheme → its §6 ladder bid level. `merchant` (post-support) has no subsidy → 0; unlisted schemes
# keep their own workbook floor (only FR carries the OA/CR schemes, so this is effectively FR-only).
_LADDER_BID = {"complement_remuneration": "cr_bid", "obligation_achat": "oa_bid", "merchant": 0.0}


def apply_oa_ladder(schemes: list[dict], ladder: dict) -> list[dict]:
    """Override a zone's RES scheme-tranche floors with the §6 downward bid ladder (`load_oa_ladder`):

      * `complement_remuneration` → `cr_bid` — the CR premium clause *suspends payment at negative prices*, so
        below zero the plant has no subsidy incentive and bids ≈0 (RES_BIDDING_DESIGN.md §Sources);
      * `obligation_achat` → `oa_bid` — the legacy feed-in tariff is *paid regardless of price*, so it produces
        however deep the price goes and bids at the market floor ("legacy OA at floor");
      * `merchant` → 0.

    Every resulting bid is truncated to `[market_floor, market_cap]` (the EU day-ahead price bounds). Shares
    and triggers are untouched — the OA *volume* still decays by vintage expiry upstream (`scheme_shares`),
    the ladder only sets the *price* each surviving tranche bids at. Schemes with no ladder entry keep their
    own (truncated) floor, so a non-FR zone passed here is only clamped, not repriced.
    """
    lo, hi = float(ladder["market_floor"]), float(ladder["market_cap"])
    out = []
    for t in schemes:
        bid = _LADDER_BID.get(t["scheme"])
        floor = float(t["floor"]) if bid is None else (float(bid) if isinstance(bid, float) else float(ladder[bid]))
        out.append({**t, "floor": min(max(floor, lo), hi)})
    return out
