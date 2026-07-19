"""Parallel MC harness — fast, DB-free unit tests (the live parallel==serial byte-identity is validated
separately on real data). Here we stub `_preload`/`project_year` to prove: draws are a deterministic
function of (master_seed, draw) so the ensemble is reproducible; a live rng source makes draws differ;
and the per-draw summary + cross-draw aggregation have the right shape."""
from __future__ import annotations

import numpy as np
import pandas as pd
from dispatch_model.rolling import montecarlo as mc


def _stub_project_year(cfg, year, ref, n_weeks=None, avail_rng=None, weather_shapes=None,
                       return_prices=False):
    """Deterministic stub: price = base(year) + a draw-specific shock from the per-draw rng."""
    base = 50.0 + (year - 2030)
    shock = float(avail_rng.normal(0, 5)) if avail_rng is not None else 0.0
    idx = pd.date_range("2019-01-01", periods=48, freq="h", tz="UTC")
    spot = pd.DataFrame({"FR": base + shock + np.sin(np.arange(48)),
                         "DE_LU": base - 5 + shock}, index=idx)
    return None, spot


def _patch(monkeypatch, avail=True):
    monkeypatch.setattr("dispatch_model.config.load_config", lambda p: object())
    monkeypatch.setattr("dispatch_model.rolling.projection._preload",
                        lambda cfg, ry, avail_years=None: {"avail_stats": {"DE_LU": {}} if avail else {}})
    monkeypatch.setattr("dispatch_model.rolling.projection.project_year", _stub_project_year)


def test_ensemble_reproducible_and_varies(monkeypatch):
    _patch(monkeypatch, avail=True)
    a = mc.run_ensemble("cfg", [2030, 2031], [0, 1, 2], avail_years=[2019], parallel=False)
    b = mc.run_ensemble("cfg", [2030, 2031], [0, 1, 2], avail_years=[2019], parallel=False)
    pd.testing.assert_frame_equal(a, b)                                   # deterministic (draw_rng keyed by draw)
    fr = a[a.zone == "FR"].groupby("draw")["mean"].mean()
    assert fr.nunique() == 3                                              # the three draws genuinely differ
    assert set(a.columns) >= {"draw", "year", "zone", "mean", "p5", "p95", "neg_hours"}
    assert sorted(a["draw"].unique()) == [0, 1, 2] and sorted(a["year"].unique()) == [2030, 2031]


def test_no_rng_source_gives_identical_draws(monkeypatch):
    _patch(monkeypatch, avail=False)                                     # avail_stats empty -> avail_rng None
    a = mc.run_ensemble("cfg", [2030], [0, 1, 2], parallel=False)
    means = a[a.zone == "FR"]["mean"].to_numpy()
    assert np.allclose(means, means[0])                                  # degenerate ensemble: all central path


def test_ensemble_stats_shape(monkeypatch):
    _patch(monkeypatch, avail=True)
    per = mc.run_ensemble("cfg", [2030], [0, 1, 2, 3], avail_years=[2019], parallel=False)
    st = mc.ensemble_stats(per)
    assert {"year", "zone", "n_draws", "ens_mean", "ens_p5", "ens_p50", "ens_p95"} <= set(st.columns)
    assert (st["n_draws"] == 4).all()
    assert (st["ens_p5"] <= st["ens_p95"] + 1e-9).all()
