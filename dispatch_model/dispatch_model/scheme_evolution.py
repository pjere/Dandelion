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

# §51 is GRANDFATHERED by commissioning vintage, not market-year-wide: pre-2016 stock (EEG ≤2014 FiT)
# has no negative-price clause at all; EEG 2017 (2016-20) = 6 h; EEG 2021 (2021-22) = 4 h; EEG 2023
# (2023-24) = 3 h; Solarspitzengesetz (from Feb 2025) = any negative hour (1 h). The 2025 fleet is a
# blend of all five classes — a single market-year trigger both under-fires the old stock and
# over-fires the new (the 2025 holdout's DE trigger chicken-and-egg, FLEX_CALIBRATION_2024.md).
_TRIGGER_BY_VINTAGE = [(2016, 0), (2021, 6), (2023, 4), (2025, 3), (9999, 1)]

RES_TECHS = ("solar", "wind_onshore", "wind_offshore", "biomass")
SUPPORT_TERM_YEARS = 20


def trigger_hours(year: int) -> int:
    """Consecutive negative hours after which the premium is cancelled, for market `year`."""
    for cutoff, hrs in _TRIGGER_BY_YEAR:
        if year < cutoff:
            return hrs
    return 1


def vintage_trigger(commissioning_year: int) -> int:
    """§51 trigger class for a plant's commissioning vintage (0 = exempt, pre-2016 stock)."""
    for cutoff, hrs in _TRIGGER_BY_VINTAGE:
        if commissioning_year < cutoff:
            return hrs
    return 1


def scheme_shares(zone: str, year: int, floors: dict[str, float],
                  new_build_mw: dict[str, float] | None = None,
                  support_term: int = SUPPORT_TERM_YEARS, reg: pd.DataFrame | None = None) -> list[dict]:
    """Year-`year` RES bid tranches for `zone`, from the registry fleet + roll-off + new build.

    `floors` = {scheme: bid_floor} (economic constants, from the workbook). Subsidised schemes take the
    year's §51 trigger; `merchant` never triggers. `new_build_mw` = {scheme: MW} added for vintages beyond
    the registry (TYNDP trajectory) — defaults to none (existing-fleet roll-off only).

    `reg` is the raw ``registry.read(zone=zone)`` frame; pass it to avoid re-reading the lake once per
    projection year (the read is year-independent — the year only enters ``active``/roll-off below). When
    None, the registry is read on demand (the standalone / test path).
    """
    from powersim_core import registry
    if reg is None:
        reg = registry.read(zone=zone)
    reg = reg[reg["tech"].isin(RES_TECHS) & reg["scheme"].notna()].copy()
    reg = registry.active(reg, year)
    reg["cap"] = pd.to_numeric(reg["capacity_mw"], errors="coerce").fillna(0.0)

    # roll-off: past the support term ⇒ merchant, whatever the statutory scheme was
    yr = pd.Timestamp(f"{year}-07-01", tz="UTC")
    end = pd.to_datetime(reg["support_end"], utc=True, errors="coerce")
    eff = reg["scheme"].where(end.isna() | (end > yr), "merchant")

    # vintage-correct §51: commissioning ≈ support_end − term ⇒ grandfathered trigger class per plant.
    # Subsidised capacity splits into per-trigger sub-tranches (same floor, scheme name suffixed @Nh);
    # plants without a support_end fall back to the market-year trigger (the pre-vintage behaviour).
    n_year = trigger_hours(year)
    vint = (end.dt.year - support_term).where(end.notna())
    trig = vint.map(lambda v: vintage_trigger(int(v)) if pd.notna(v) else n_year)
    trig = trig.where(eff != "merchant", 0).astype(int)
    by_key = reg.groupby([eff, trig])["cap"].sum().to_dict()

    for scheme, mw in (new_build_mw or {}).items():        # future vintages under the prevailing scheme
        k = (scheme, 0 if scheme == "merchant" else vintage_trigger(year))
        by_key[k] = by_key.get(k, 0.0) + float(mw)

    total = sum(by_key.values()) or 1.0
    out = []
    for (scheme, n), mw in sorted(by_key.items(), key=lambda kv: -kv[1]):
        name = scheme if (n == 0 or scheme == "merchant") else f"{scheme}@{n}h"
        out.append({"scheme": name, "share": mw / total,
                    "floor": float(floors.get(scheme, 0.0)),
                    "trigger": int(n)})
    return out
