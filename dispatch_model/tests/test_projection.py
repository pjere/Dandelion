"""Projection-mode input scaling (capacity adequacy, growth loading)."""
from __future__ import annotations

import pandas as pd
from dispatch_model.rolling.projection import _GROWTH, _scale_stack


def _stack():
    return pd.DataFrame({
        "unit_id": ["c1", "l1", "g1", "g2", "n1"],
        "tech": ["coal", "lignite", "gas", "gas", "nuclear"],
        "capacity_mw": [5000.0, 5000.0, 4000.0, 4000.0, 6000.0],
    })


def test_retired_coal_and_lignite_is_replaced_by_gas_preserving_firm_capacity():
    st = _stack()
    firm0 = st.loc[st["tech"].isin(["coal", "lignite", "gas"]), "capacity_mw"].sum()
    g = dict(_GROWTH, coal=-0.08, lignite=-0.08)
    out = _scale_stack(st, k=21, g=g)                          # ~2040

    coal = out.loc[out["tech"].isin(["coal", "lignite"]), "capacity_mw"].sum()
    orig_coal = st.loc[st["tech"].isin(["coal", "lignite"]), "capacity_mw"].sum()
    assert coal < 0.2 * orig_coal                             # ~83 % retired
    # the retired MW landed in gas → total dispatchable-thermal firm capacity is preserved (adequacy)
    firm1 = out.loc[out["tech"].isin(["coal", "lignite", "gas"]), "capacity_mw"].sum()
    assert firm1 == pytest_approx(firm0)
    assert out.loc[out["tech"] == "gas", "capacity_mw"].sum() > st.loc[st["tech"] == "gas", "capacity_mw"].sum()


def test_no_growth_is_identity():
    st = _stack()
    out = _scale_stack(st, k=0, g=dict(_GROWTH))
    pd.testing.assert_frame_equal(out, st)


def pytest_approx(x, rel=1e-6):
    import pytest
    return pytest.approx(x, rel=rel)
