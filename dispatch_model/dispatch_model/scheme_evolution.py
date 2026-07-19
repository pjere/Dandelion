"""Year-varying RES subsidy mix — makes the bid stack projection-valid (step vii).

The static `dispatch_res_schemes` shares describe *today*. Over a 20-year price simulation the fleet
turns over and the negative-price behaviour changes for legible legal reasons, all **derived, not fitted**:

  * **roll-off** — a plant is `merchant` (bids ≈0) once `support_end = commissioning + 20y ≤ projection_year`.
    2019-vintage FiT solar is merchant by 2039. Its deep −€60 floor disappears from the stack.
  * **new build** — capacity added in year Y enters under the *prevailing* scheme for that vintage
    (EEG 2027+ ⇒ CfD + mandatory direct marketing ⇒ bids ≈0, 1-hour trigger).
  * **trigger tightening** — §51 EEG: 6 h (≤2020) → 4 h (2021-24) → 3 h (2025) → 2 h (2026) → 1 h (2027+).

Net effect: the deep-subsidy tranches shrink and the ≈0 / 1-hour tranches grow, so future negatives get
**shallower and shorter**. Freezing the 2019 mix (the static tab) would instead carry deep floors + a 6-hour
trigger to 2046 — systematically over-deep, over-frequent future negatives. The fleet turnover is read from
the plant registry (`support_end`, `scheme`, `active(year)` — ADR-7); floors stay the economic constants
from the workbook. See docs/RES_BIDDING_DESIGN.md §6e.
"""
from __future__ import annotations

import pandas as pd

# §51 EEG (and analogous CfD) consecutive-negative-hours trigger, by commissioning-independent market year.
_TRIGGER_BY_YEAR = [(2021, 6), (2025, 4), (2026, 3), (2027, 2), (9999, 1)]

RES_TECHS = ("solar", "wind_onshore", "wind_offshore", "biomass")
SUPPORT_TERM_YEARS = 20


def trigger_hours(year: int) -> int:
    """Consecutive negative hours after which the premium is cancelled, for market `year`."""
    for cutoff, hrs in _TRIGGER_BY_YEAR:
        if year < cutoff:
            return hrs
    return 1


def scheme_shares(zone: str, year: int, floors: dict[str, float],
                  new_build_mw: dict[str, float] | None = None,
                  support_term: int = SUPPORT_TERM_YEARS) -> list[dict]:
    """Year-`year` RES bid tranches for `zone`, from the registry fleet + roll-off + new build.

    `floors` = {scheme: bid_floor} (economic constants, from the workbook). Subsidised schemes take the
    year's §51 trigger; `merchant` never triggers. `new_build_mw` = {scheme: MW} added for vintages beyond
    the registry (TYNDP trajectory) — defaults to none (existing-fleet roll-off only).
    """
    from powersim_core import registry
    reg = registry.read(zone=zone)
    reg = reg[reg["tech"].isin(RES_TECHS) & reg["scheme"].notna()].copy()
    reg = registry.active(reg, year)
    reg["cap"] = pd.to_numeric(reg["capacity_mw"], errors="coerce").fillna(0.0)

    # roll-off: past the support term ⇒ merchant, whatever the statutory scheme was
    yr = pd.Timestamp(f"{year}-07-01", tz="UTC")
    end = pd.to_datetime(reg["support_end"], utc=True, errors="coerce")
    eff = reg["scheme"].where(end.isna() | (end > yr), "merchant")
    by_scheme = reg.groupby(eff)["cap"].sum().to_dict()

    for scheme, mw in (new_build_mw or {}).items():        # future vintages under the prevailing scheme
        by_scheme[scheme] = by_scheme.get(scheme, 0.0) + float(mw)

    total = sum(by_scheme.values()) or 1.0
    n = trigger_hours(year)
    out = []
    for scheme, mw in sorted(by_scheme.items(), key=lambda kv: -kv[1]):
        out.append({"scheme": scheme, "share": mw / total,
                    "floor": float(floors.get(scheme, 0.0)),
                    "trigger": 0 if scheme == "merchant" else n})
    return out
