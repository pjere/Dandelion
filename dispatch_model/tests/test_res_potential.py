"""`flexibility.res_potential.solar_uplift` — the censored-solar estimator and its noise floor.

The estimator's whole job is to separate CURTAILMENT from WEATHER. Its noise floor was a per-hour MEDIAN
of the same-hour dip on uncensored hours, and a median is the centre of the weather-variance distribution
rather than a floor — so half of every uncensored hour's dip survived as "censored potential". Measured on
2024 that booked 46 TWh/yr of zero-cost must-take across the neighbour zones, 70-72 % of it on hours the
market priced at or above 50 EUR/MWh.

The decisive property is the PLACEBO: on a series containing no curtailment at all, the estimator must
return ~nothing. It returned 102-150 % of its real-data output. That property is pinned here.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from dispatch_model.flexibility import res_potential as rp


def _solar_year(days: int = 120, peak: float = 8000.0, seed: int = 0):
    """A curtailment-free solar year: clear-sky diurnal shape × a random daily cloud factor.

    This is the placebo. Every dip in it is weather; none of it is curtailment.
    """
    idx = pd.date_range("2024-03-01", periods=24 * days, freq="h", tz="UTC")
    rng = np.random.default_rng(seed)
    h = idx.hour.to_numpy()
    clear = np.clip(np.sin(np.pi * (h - 5) / 14.0), 0.0, None) ** 1.5 * peak
    cloud = np.repeat(rng.uniform(0.25, 1.0, days), 24)          # one cloud factor per day
    return pd.DataFrame({"timestamp_utc": idx, "tech": "solar", "gen_mw": clear * cloud}), idx, clear


def _gen_frame(idx, values):
    return pd.DataFrame({"timestamp_utc": idx, "tech": "solar", "gen_mw": values})


def test_no_prices_means_no_uplift():
    """The old `prices=None` branch set the floor to 0, so the FULL dip counted as curtailment —
    maximum uplift exactly where there is least evidence. It fired silently for DK/PL_CZ/AT_SI/IT_SOUTH."""
    gen, _, _ = _solar_year()
    assert rp.solar_uplift(gen, None).empty


def test_placebo_a_curtailment_free_year_yields_almost_nothing():
    """THE property. No curtailment in the input ⇒ essentially no uplift out."""
    gen, idx, _ = _solar_year(seed=1)
    prices = pd.Series(60.0, index=idx)                          # never negative: nobody curtails
    up = rp.solar_uplift(gen, prices)
    produced = float(gen["gen_mw"].sum())
    assert float(up.sum()) < 0.03 * produced, (
        f"placebo leak: {100 * up.sum() / produced:.1f} % of production invented as curtailment")


def test_genuine_curtailment_is_still_recovered():
    """The floor must not be so high that the estimator goes blind — it still has a job to do.

    The threshold is the MEASURED operating point, not a round number. Sweeping the floor on these two
    synthetic years (placebo leak = uplift as a share of production on a curtailment-free year; recovery =
    share of injected curtailment found; spill = false uplift on uncensored hours, as a share of the real
    signal):

        floor q     0.50    0.75    0.85    0.90    0.95    0.99
        leak %     15.78    3.48    1.63    0.70    0.31    0.01
        recovery %  90.0    45.7    29.9    25.2    15.2    13.4
        spill %    191.1    51.9    18.7     9.9     1.4     0.1

    The old q50 recovers most of the real signal but invents nearly TWICE as much again on clean hours —
    and on real data most hours are uncensored, which is why it cost 46 TWh/yr. q90 trades recovery
    90 % -> 25 % to cut spill 191 % -> 10 %. Under-recovering real curtailment is the safe direction;
    inventing it is not.
    """
    gen, idx, clear = _solar_year(seed=2)
    curtailed = gen["gen_mw"].to_numpy().copy()
    # 20 midday blocks clamped to 30 % of what that hour would otherwise have produced
    hit = np.zeros(len(idx), bool)
    for d in range(5, 105, 5):
        sl = slice(d * 24 + 10, d * 24 + 15)
        hit[sl] = True
        curtailed[sl] *= 0.30
    prices = pd.Series(60.0, index=idx)
    prices[hit] = -20.0                                          # the curtailed blocks priced negative
    up = rp.solar_uplift(_gen_frame(idx, curtailed), prices)
    lost = float((gen["gen_mw"].to_numpy() - curtailed)[hit].sum())
    got = float(up.reindex(idx).fillna(0.0).to_numpy()[hit].sum())
    assert got > 0.20 * lost, f"recovered only {100 * got / lost:.0f} % of injected curtailment"
    # and it must not smear onto the uncensored hours
    spill = float(up.reindex(idx).fillna(0.0).to_numpy()[~hit].sum())
    assert spill < 0.5 * got, (
        f"false uplift on uncensored hours is {100 * spill / got:.0f} % of the true signal; "
        "at the old q50 floor it was 191 %")


def test_noise_floor_is_an_upper_quantile_not_a_median():
    """Pins the fix itself. A median floor leaves ~half of every uncensored dip above it by construction."""
    assert rp._NOISE_Q >= 0.85, "the noise floor must sit in the tail of the weather-variance distribution"
    gen, idx, _ = _solar_year(seed=3)
    prices = pd.Series(60.0, index=idx)

    def uplift_at(q):
        old = rp._NOISE_Q
        try:
            rp._NOISE_Q = q
            return float(rp.solar_uplift(gen, prices).sum())
        finally:
            rp._NOISE_Q = old

    # monotone in the floor, and the median setting is dramatically leakier than the shipped one
    assert uplift_at(0.50) > 5 * uplift_at(rp._NOISE_Q) > 0.0 or uplift_at(rp._NOISE_Q) == 0.0
    assert uplift_at(0.50) > uplift_at(0.90) >= uplift_at(0.99)


def test_uplift_is_never_negative_and_indexed_like_the_input():
    gen, idx, _ = _solar_year(seed=4)
    up = rp.solar_uplift(gen, pd.Series(60.0, index=idx))
    assert (up >= -1e-9).all()
    assert up.index.equals(pd.DatetimeIndex(idx))
