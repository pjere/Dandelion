"""Zone- and period-specific gas rules: hub basis over TTF, and regulatory caps on gas-for-power.

Both are **missing inputs, not behavioural wedges** — the same argument as the flat gas-basis fix in
`commodities.model`. If they are left out, the step-vii markup absorbs them as a fake "Spanish discount"
or "Italian premium" and then carries that fiction into every projected year, where neither regime exists.

Two mechanisms:

* **Hub basis** — zones do not all burn TTF gas. IT-North prices off PSV and ES off MIBGAS. The flat basis
  in `load_zone_basis` is a reasonable long-run average but was badly wrong in 2022, when PSV-TTF widened
  far beyond its normal ~€2. This module makes the basis **period-keyed** so a real hub series can be
  dropped in; the shipped defaults keep the existing flat values, so nothing moves until the table is
  filled with sourced values.

* **Iberian exception** (Spain/Portugal, RDL 10/2022) — from **15 June 2022** the gas price *used to set
  the power-market marginal cost* in Iberia was capped, with generators compensated separately. So the
  merit order cleared against a capped fuel cost while TTF traded €200+. Modelling ES gas at TTF through
  H2-2022 therefore over-costs every Spanish CCGT and pushes ES SMC well above what actually cleared.
  Schedule: €40/MWh_th for the first six months, then +€5/MWh_th per month (≈€48.8 average over the first
  twelve), and the mechanism ran to 31 December 2023.

Only the core RDL 10/2022 schedule is asserted with confidence; the 2023 extension level is an
**approximation** and, like everything here, is overridable from the `dispatch_gas_rules` workbook tab
(columns: zone, from_date, to_date, basis_eur_mwhth, cap_eur_mwhth).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd


@dataclass(frozen=True)
class GasRule:
    zone: str
    from_date: pd.Timestamp
    to_date: pd.Timestamp
    basis_eur_mwhth: float | None = None       # additive over TTF (None => fall back to the flat basis)
    cap_eur_mwhth: float | None = None         # regulatory ceiling on gas-for-power (Iberian exception)


def _iberian_cap_schedule() -> list[tuple]:
    """(from, to, cap) for the Iberian exception: €40 for six months, then +€5/month; extended to end-2023."""
    rows = [("2022-06-15", "2022-12-14", 40.0)]
    start = pd.Timestamp("2022-12-15")
    cap = 45.0
    while cap <= 70.0:                                   # months 7-12: 45, 50, 55, 60, 65, 70
        rows.append((start.strftime("%Y-%m-%d"),
                     (start + pd.DateOffset(months=1) - pd.Timedelta(days=1)).strftime("%Y-%m-%d"), cap))
        start += pd.DateOffset(months=1)
        cap += 5.0
    # extension of the mechanism to 31-Dec-2023 — level approximate, workbook-overridable
    rows.append((start.strftime("%Y-%m-%d"), "2023-12-31", 70.0))
    return rows


#: shipped defaults. Basis entries are deliberately absent (the flat `dispatch_zone_basis` still applies)
#: so the only behavioural change from this module is the documented Iberian cap.
DEFAULT_GAS_RULES: list[GasRule] = [
    GasRule("ES", pd.Timestamp(a, tz="UTC"), pd.Timestamp(b, tz="UTC"), None, cap)
    for a, b, cap in _iberian_cap_schedule()
]


def load_gas_rules(workbook) -> list[GasRule]:
    """Rules from the `dispatch_gas_rules` tab; the built-in Iberian schedule when the tab is absent."""
    p = Path(str(workbook)) if workbook else None
    if not p or not p.exists():
        return list(DEFAULT_GAS_RULES)
    try:
        from powersim_core.scenario import load_sheet
        df = load_sheet(p, "dispatch", "gas_rules")
    except (ValueError, KeyError):
        return list(DEFAULT_GAS_RULES)
    out = []
    for r in df.itertuples():
        out.append(GasRule(
            zone=str(r.zone),
            from_date=pd.Timestamp(r.from_date, tz="UTC"), to_date=pd.Timestamp(r.to_date, tz="UTC"),
            basis_eur_mwhth=(float(r.basis_eur_mwhth) if pd.notna(getattr(r, "basis_eur_mwhth", None)) else None),
            cap_eur_mwhth=(float(r.cap_eur_mwhth) if pd.notna(getattr(r, "cap_eur_mwhth", None)) else None)))
    return out or list(DEFAULT_GAS_RULES)


def gas_rule_at(rules: list[GasRule], zone: str, ts) -> GasRule | None:
    """The rule in force for `zone` at `ts` (last match wins, so later rows override earlier ones)."""
    if not rules or ts is None:
        return None
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")
    hit = None
    for r in rules:
        if r.zone == zone and r.from_date <= t <= r.to_date:
            hit = r
    return hit


def adjust_gas(ttf: float, zone: str, ts, rules: list[GasRule] | None, flat_basis: float = 0.0) -> float:
    """TTF → the gas price this zone's plant actually faces: hub basis first, then any regulatory cap."""
    r = gas_rule_at(rules or [], zone, ts)
    basis = flat_basis if (r is None or r.basis_eur_mwhth is None) else r.basis_eur_mwhth
    price = float(ttf) + float(basis)
    if r is not None and r.cap_eur_mwhth is not None:
        price = min(price, float(r.cap_eur_mwhth))       # gas-for-power ceiling (Iberian exception)
    return price
