"""Market *rules* per zone and period — not economics, not fitted parameters.

A zone-year where negative prices were **prohibited by regulation** must be floored at 0, or the model
invents prices the market could not have printed:

  * **IT-North** — negative prices prohibited until the TIDE reform (**Jan-2025**).
  * **ES** — negative prices only permitted from **Dec-2023**.

Their zero observed negative hours in 2019 are therefore a *rule*, not an outcome (the model currently
prints 6 spurious negative ES hours). Both floors are **gone** over the 2027-46 projection horizon, so
history and future run under different rules — which is exactly why this cannot be folded into a
step-(vii) markup fitted on floored history.

Rules live in the `dispatch_price_rules` tab of `scenarios.xlsx` (zone/from_date/to_date/res_bid/
price_floor); the `DEFAULT` row applies to any zone not otherwise listed.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd

DEFAULT_RES_BID = -10.0
DEFAULT_PRICE_FLOOR = -500.0
_DEFAULT_KEY = "DEFAULT"


@lru_cache(maxsize=8)
def load_price_rules(workbook: str) -> pd.DataFrame:
    """→ [zone, from_date, to_date, res_bid_eur_mwh, price_floor_eur_mwh]; empty if the tab is absent."""
    cols = ["zone", "from_date", "to_date", "res_bid_eur_mwh", "price_floor_eur_mwh"]
    if not workbook or not Path(workbook).exists():
        return pd.DataFrame(columns=cols)
    try:
        from powersim_core.scenario import load_sheet
        df = load_sheet(workbook, "dispatch", "price_rules")
    except (ValueError, KeyError):
        return pd.DataFrame(columns=cols)
    df = df.copy()
    for c in ("from_date", "to_date"):
        df[c] = pd.to_datetime(df[c], utc=True)
    return df[cols]


def rules_at(workbook: str, ts, zones) -> tuple[dict, dict]:
    """(res_bid, price_floor) per zone effective at `ts`. Zone row wins over DEFAULT."""
    ts = pd.Timestamp(ts)
    ts = ts.tz_localize("UTC") if ts.tz is None else ts.tz_convert("UTC")
    df = load_price_rules(str(workbook) if workbook else "")
    live = df[(df["from_date"] <= ts) & (df["to_date"] >= ts)] if not df.empty else df

    dflt = live[live["zone"] == _DEFAULT_KEY]
    d_bid = float(dflt["res_bid_eur_mwh"].iloc[0]) if not dflt.empty else DEFAULT_RES_BID
    d_floor = float(dflt["price_floor_eur_mwh"].iloc[0]) if not dflt.empty else DEFAULT_PRICE_FLOOR

    bids, floors = {}, {}
    for z in zones:
        row = live[live["zone"] == z] if not live.empty else live
        bids[z] = float(row["res_bid_eur_mwh"].iloc[0]) if len(row) else d_bid
        floors[z] = float(row["price_floor_eur_mwh"].iloc[0]) if len(row) else d_floor
    return bids, floors
