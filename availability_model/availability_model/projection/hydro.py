"""Phase 5 — reservoir hydro energy budget over the horizon.

Dispatchable reservoir/lake hydro is an energy-constrained resource, not an equipment-outage process:
what the price model needs is the seasonal ceiling on available stored energy. We take the calibrated
usable capacity and weekly fill climatology (drawn down over winter, refilled by spring snowmelt) and
scale by a wetness factor from the SAME weather draw (a dry year lowers the ceiling). Run-of-river and
reservoir *production* stay with res_model (iv) / the dispatch in step (vi) — this only sets the budget.
"""
from __future__ import annotations

import pandas as pd

from ..calibration.model import CalibratedAvailability
from ..config import Config


def reservoir_energy_budget(config: Config, model: CalibratedAvailability,
                            wetness_by_year: dict[int, float] | float = 1.0) -> pd.DataFrame:
    """Weekly available reservoir energy (GWh) over the horizon → [week_start, avail_energy_gwh].

    `wetness_by_year` scales the ceiling per calendar year (from the shared precip draw); a scalar
    applies uniformly. Clipped to a physical floor so the reservoir is never fully unavailable.
    """
    proj = config.section("projection")
    y0, y1 = proj["horizon"]["start_year"], proj["horizon"]["end_year"]
    res = model.inflows["reservoir"]
    usable = float(res["usable_energy_gwh"])
    floor = float(res.get("min_stock_gwh", 0.0))
    prof = {int(k): float(v) for k, v in res["seasonal_profile_week"].items()}

    weeks = pd.date_range(pd.Timestamp(year=y0, month=1, day=1),
                          pd.Timestamp(year=y1, month=12, day=31), freq="W-MON", tz="UTC")
    rows = []
    for wk in weeks:
        iso = min(int(pd.Timestamp(wk).isocalendar().week), 52)
        wet = wetness_by_year.get(wk.year, 1.0) if isinstance(wetness_by_year, dict) else float(wetness_by_year)
        energy = floor + usable * prof.get(iso, 1.0) * wet
        rows.append({"week_start": wk, "avail_energy_gwh": round(max(floor, energy), 1)})
    return pd.DataFrame(rows)
