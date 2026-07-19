"""DE unit-level stack (#73): vintage-efficiency logic (pure); registry build is an integration smoke."""
from __future__ import annotations

import pytest
from dispatch_model.neighbours.blocks import _vintage_efficiency
from dispatch_model.stacks.costs import EFF_RANGE


def test_vintage_efficiency_within_band_and_monotone():
    lo, hi = EFF_RANGE.get("gas", (0.35, 0.58))
    old = _vintage_efficiency("gas", "1975-01-01")
    new = _vintage_efficiency("gas", "2018-01-01")
    assert lo <= old <= hi and lo <= new <= hi
    assert new > old                                    # newer plant is more efficient
    assert abs(_vintage_efficiency("gas", None) - 0.5 * (lo + hi)) < 1e-9   # missing date → band midpoint


def test_vintage_efficiency_clamps_pre1970_and_post2020():
    lo, hi = EFF_RANGE.get("coal", (0.30, 0.45))
    assert abs(_vintage_efficiency("coal", "1960-01-01") - lo) < 1e-9       # clamps to band floor
    assert abs(_vintage_efficiency("coal", "2030-01-01") - hi) < 1e-9       # clamps to band ceiling


@pytest.mark.integration
def test_build_de_unit_stack_schema_and_size():
    """Integration smoke against the real MaStR registry (skips if unavailable)."""
    from dispatch_model.config import load_config
    from dispatch_model.neighbours.blocks import build_de_unit_stack
    try:
        st = build_de_unit_stack(load_config("config.yaml"), "DE_LU", 2019)
    except Exception as e:  # noqa: BLE001 — no registry in this environment
        pytest.skip(f"registry unavailable: {e}")
    assert {"unit_id", "zone", "tech", "capacity_mw", "efficiency", "min_gen_frac"}.issubset(st.columns)
    assert 50 < len(st) < 1000                          # individual large units + small aggregates
    assert 50_000 < st["capacity_mw"].sum() < 130_000   # ~90 GW DE thermal, availability-derated
    assert st.loc[st["tech"] == "gas", "efficiency"].nunique() > 5   # finer than 3 sub-blocks
