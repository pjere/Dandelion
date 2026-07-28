"""RES-potential reconstruction (the depth unlock, v1: solar only, neighbours only).

The model's must-take RES is *observed* generation — post-curtailment — so exactly on the surplus hours
that price negative, the input understates the true potential and the modelled surplus is too shallow to
reach the deep subsidy floors. Measured (`scratchpad/res_censoring.py`, 2024): the same-hour ±7-day
envelope dip concentrates on observed-negative hours for **solar in every zone** (dip@neg / dip@pos ratios
2.0–4.9; DE 2.8 TWh censored on negative hours alone) but **not for wind** (ratios 0.6–1.0 — calm weather
correlates with positive prices, so the envelope cannot separate curtailment from weather; wind
reconstruction is honestly excluded even though real, e.g. German EinsMan).

Estimator (leakage boundary, deliberate): the uplift is **price-unconditioned per hour** —
`uplift_h = max(0, dip_h − noise[zone, hour-of-day])` with `dip_h = envelope_h − obs_h`. Observed prices
enter only the per-(zone, hod) `noise` constant (the estimator's weather-variance floor, measured on
clearly-positive hours) — never to place uplift on specific hours, which would inject the answer.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

_SOLAR = ("solar",)
_WINDOW_DAYS = 15            # ±7-day same-hour envelope
_POS_PRICE = 5.0             # "clearly uncensored" reference hours for the noise floor


def solar_uplift(gen: pd.DataFrame, prices: pd.Series | None) -> pd.Series:
    """→ hourly MW to ADD to a zone's must-take RES: the censored solar potential estimate.

    `gen` = the zone's `load_generation_hist` frame (timestamp_utc/tech/gen_mw); `prices` = the zone's
    observed day-ahead series (None ⇒ noise floor 0 — conservative over-uplift, flagged for projection use).
    """
    s = (gen[gen["tech"].isin(_SOLAR)].groupby("timestamp_utc")["gen_mw"].sum().asfreq("h"))
    if s.dropna().empty:
        return pd.Series(dtype=float)
    df = pd.DataFrame({"gen": s})
    df["hod"] = df.index.hour
    df["day"] = (df.index - df.index[0]).days
    piv = df.pivot_table(index="day", columns="hod", values="gen")
    env = piv.rolling(_WINDOW_DAYS, center=True, min_periods=5).max().stack()
    envs = pd.Series([env.get((d, h), np.nan) for d, h in zip(df["day"], df["hod"])], index=df.index)
    dip = (envs - df["gen"]).clip(lower=0).fillna(0.0)
    if prices is not None:
        pos = prices.reindex(df.index) >= _POS_PRICE
        noise = dip[pos].groupby(df.loc[pos, "hod"]).median()
        floor = df["hod"].map(noise).fillna(0.0)
    else:
        floor = 0.0
    return (dip - floor).clip(lower=0).rename("solar_uplift_mw")
