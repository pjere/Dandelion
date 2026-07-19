"""RES Phase 2 tests: cloud→GHI is physical, wind transfer recovers a known shear + predicts well."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from res_model.config import load_config
from res_model.transfer.ghi import ghi_from_cloud
from res_model.transfer.wind import apply_wind_transfer, fit_wind_transfer, transfer_quality_vs_era5


def test_ghi_from_cloud_physical():
    # a July day at ~46°N: clear sky > overcast, zero at night, positive at midday
    times = pd.date_range("2025-07-01", periods=24, freq="h", tz="UTC")
    clear = ghi_from_cloud(times, 46.0, 2.0, np.zeros(24))
    overcast = ghi_from_cloud(times, 46.0, 2.0, np.full(24, 100.0))
    assert clear.max() > 700 and clear.max() < 1050          # sane summer clear-sky peak
    assert (clear >= overcast - 1e-9).all()                  # clouds never increase GHI
    assert clear.iloc[0] == 0.0 and clear.iloc[23] == 0.0    # night = 0 (UTC ~ solar night)
    assert overcast.max() < 0.35 * clear.max()               # heavy overcast strongly attenuates


def test_wind_transfer_recovers_shear():
    rng = np.random.default_rng(0)
    idx = pd.date_range("2020-01-01", periods=24 * 365, freq="h", tz="UTC")
    w10 = pd.Series(np.abs(rng.weibull(2.0, len(idx)) * 5.0) + 0.2, index=idx)
    # true relation: w100 = 1.6 · w10^1.05 with a small lag-1 persistence + lognormal noise
    lag1 = w10.shift(1).fillna(w10.mean())
    w100 = 1.6 * w10 ** 1.05 * (lag1 / lag1.mean()) ** 0.1 * np.exp(rng.normal(0, 0.08, len(idx)))
    w100 = pd.Series(w100, index=idx)

    m = fit_wind_transfer(w10.iloc[: 24 * 300], w100.iloc[: 24 * 300])
    assert m.r2 > 0.9                                         # log-linear transfer fits well
    assert 0.9 < m.coef[1] < 1.2                             # recovers ~shear exponent 1.05
    # out-of-sample: predicted 100 m wind tracks the held-out truth
    pred = apply_wind_transfer(m, w10.iloc[24 * 300:])
    truth = w100.reindex(pred.index)
    assert np.corrcoef(pred.to_numpy(), truth.to_numpy())[0, 1] > 0.93
    assert pred.mean() > 1.5 * w10.mean()                    # hub-height wind uplifted vs 10 m


def test_transfer_quality_verdict():
    assert transfer_quality_vs_era5(0.80, 0.82).startswith("OK")
    assert transfer_quality_vs_era5(0.60, 0.80).startswith("FLAG")


def test_fitted_transfers_bundle_present():
    """If the ERA5 extract was fitted, the saved bundle has an onshore transfer + cross-check."""
    from res_model.transfer.build import load_wind_transfers
    cfg = load_config("config.yaml")
    p = cfg.models_dir / "wind_transfers.json"
    if not p.exists():
        pytest.skip("wind_transfers.json not built yet (needs ERA5 extract)")
    b = load_wind_transfers(p)                                # portable JSON (no pickle — REVIEW F6)
    assert b["onshore"].r2 > 0.5 and 0.8 < b["onshore"].coef[1] < 1.6
    assert "verdict" in b["crosscheck"] and b["offshore"]
