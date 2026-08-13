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

#: Quantile of the same-hour dip, over clearly-uncensored hours, that defines the weather-variance floor.
#:
#: THIS WAS A MEDIAN, AND A MEDIAN IS NOT A FLOOR. The noise term exists to subtract off ordinary
#: cloud variability so that what survives is censored potential. Taking its CENTRE leaves half of every
#: uncensored hour's dip above the floor by construction, so the estimator books routine cloud cover as
#: curtailment on hours nobody curtails. Measured on 2024 across the 12 neighbour zones:
#:
#:     floor quantile     0.50 (was)    0.75    0.90 (now)    0.95    0.99
#:     total uplift TWh        29.0     11.6          3.6      1.5     0.2
#:
#: Three independent checks say the old level was ~10x too high. (1) 70-72 % of the uplift landed on hours
#: the market priced at or above 50 EUR/MWh. (2) The estimator returned the same 16-25 % uplift in
#: zone-years where curtailment was IMPOSSIBLE because the zone never priced negative — IT_NORTH has zero
#: negative hours in 2019, 2022, 2024 and 2025 yet was uplifted every year. (3) A curtailment-free
#: synthetic placebo reproduced 102-150 % of the real uplift in all 14 zone-years tested.
#:
#: 0.90 is calibrated against an independent bound on the genuine signal — the negative-hour dip in excess
#: of the hour-of-day-matched positive-hour mean — which gives DE_LU 0.99 / ES 0.65 / GB 0.07 / IT_NORTH
#: 0.00 TWh. At q90 the estimator returns 1.29 / 0.77 / 0.26 / 0.39, the closest match of any quantile
#: tested; q95 undershoots DE_LU and ES by about half.
_NOISE_Q = 0.90


def solar_uplift(gen: pd.DataFrame, prices: pd.Series | None) -> pd.Series:
    """→ hourly MW to ADD to a zone's must-take RES: the censored solar potential estimate.

    `gen` = the zone's `load_generation_hist` frame (timestamp_utc/tech/gen_mw); `prices` = the zone's
    observed day-ahead series.

    NO PRICES ⇒ NO UPLIFT. The previous behaviour was `floor = 0`, i.e. the FULL dip counted as
    curtailment — maximum uplift exactly where there is least information to justify it, described in the
    docstring as "conservative" when it is the opposite. It was firing silently in the backtest for DK,
    PL_CZ, AT_SI and IT_SOUTH (+12.05 TWh in 2025 alone), because `_observed_prices` looks up the virtual
    zone key while the lake stores prices under the constituent keys. The call site now aggregates
    constituent prices; where none exist, this returns empty rather than substituting a default for
    missing data.
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
    if prices is None:
        return pd.Series(dtype=float)              # no reference hours ⇒ no defensible floor ⇒ no uplift
    pos = prices.reindex(df.index) >= _POS_PRICE
    if not bool(pos.any()):
        return pd.Series(dtype=float)
    noise = dip[pos].groupby(df.loc[pos, "hod"]).quantile(_NOISE_Q)
    floor = df["hod"].map(noise).fillna(0.0)
    return (dip - floor).clip(lower=0).rename("solar_uplift_mw")


_BTM_DERATE = 0.85           # rooftop vs utility shape: orientation/pitch mix + soiling (physical prior;
#                              cross-check: 27 GW BTM × NL solar CF ~0.115 × 0.85 ≈ 23 TWh/yr ≈ the CBS
#                              statistical total 2025 that the ENTSO-E feed almost entirely lacks)
_BTM_NETTED = 0.05           # fraction of rooftop production ALREADY netted in the ENTSO-E load series —
#                              measured (scratchpad/nl_netting_regression.py, within month×hod×weekend
#                              cells): φ = 0.066 (2024) / 0.037 (2025), se 0.003. The load is ~gross;
#                              the wholesale addition is (1 − φ) × production.


def btm_solar(gen: pd.DataFrame, installed_solar_mw: float) -> pd.Series:
    """→ hourly MW of behind-the-meter solar to ADD to a zone's must-take RES (the NL salderen fix).

    The NL ENTSO-E feed carries only the metered utility fleet (~0.2 GW mean vs 29.3 GW installed 2025)
    and the load series does not net the rooftop fleet either (measured: net load does NOT collapse on
    observed-negative noons) — the real Dutch surplus is invisible on BOTH sides of the balance
    (2025 decomposition: model-NL prices ~88 on obs-negative hours, demand 12.2 vs must-take 2.3 GW).
    Reconstruction: the metered utility series provides the zone's irradiance shape; the invisible
    capacity (installed − utility p99.9) rides that shape at `_BTM_DERATE`. Year-correct via the
    installed-capacity input (nearest-year fallback upstream). Returns an empty series when the metered
    fleet is too small to carry a shape (<50 MW) or nothing is invisible."""
    s = (gen[gen["tech"].isin(_SOLAR)].groupby("timestamp_utc")["gen_mw"].sum().asfreq("h"))
    if s.dropna().empty:
        return pd.Series(dtype=float)
    utility_cap = float(s.quantile(0.999))
    btm_cap = max(float(installed_solar_mw) - utility_cap, 0.0)
    if utility_cap < 50.0 or btm_cap <= 0.0:
        return pd.Series(dtype=float)
    shape = (s / utility_cap).clip(lower=0.0, upper=1.0)
    return (shape * btm_cap * _BTM_DERATE * (1.0 - _BTM_NETTED)).fillna(0.0).rename("btm_solar_mw")
