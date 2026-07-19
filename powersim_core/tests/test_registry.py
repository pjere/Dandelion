"""Asset master (ADR-7): canonical schema, provenance, and the registry ⊕ overrides split."""
from __future__ import annotations

import pandas as pd
import pytest

from powersim_core import registry


@pytest.fixture
def isolated_lake(tmp_path, monkeypatch):
    monkeypatch.setenv("POWERSIM_LAKE", str(tmp_path / "lake"))
    return tmp_path


def _src(n=3, source="mastr"):
    return pd.DataFrame({
        "source": source,
        "source_id": [f"U{i}" for i in range(n)],
        "as_of": "2026-07-01",
        "zone": "DE_LU",
        "tech": ["lignite", "gas", "solar"][:n],
        "capacity_mw": [900.0, 400.0, 12.5][:n],
        "commissioning_date": ["1985-06-01", "2012-03-01", "2019-09-01"][:n],
        "chp_flag": [False, True, False][:n],
    })


def test_normalise_fills_schema_and_derives_stable_id():
    out = registry.normalise(_src())
    assert list(out.columns) == registry.COLUMNS          # exact canonical schema, extras dropped
    assert list(out["plant_id"]) == ["mastr:U0", "mastr:U1", "mastr:U2"]
    assert out["capacity_mw"].dtype.kind == "f"
    assert str(out["commissioning_date"].dtype).startswith("datetime64")
    assert out["efficiency_est"].isna().all()             # modelled later, never from the registry


def test_write_read_roundtrip_partitioned_by_source(isolated_lake):
    registry.write(_src(source="mastr"), "mastr")
    registry.write(_src(n=2, source="odre").assign(zone="FR"), "odre")
    assert len(registry.read(source="mastr")) == 3
    assert len(registry.read()) == 5                      # every source concatenated
    assert set(registry.read(zone="FR")["source"]) == {"odre"}


def test_overrides_update_retire_and_add_without_touching_source_truth(isolated_lake):
    reg = registry.normalise(_src())
    ov = pd.DataFrame({
        "plant_id": ["mastr:U0", "mastr:U1", "workbook:NEW1"],
        "source": ["workbook"] * 3,
        "source_id": ["U0", "U1", "NEW1"],
        "retirement_date": ["2030-12-31", pd.NA, pd.NA],   # scenario closure
        "capacity_mw": [pd.NA, 450.0, 800.0],              # uprate + new build
        "zone": [pd.NA, pd.NA, "DE_LU"],
        "tech": [pd.NA, pd.NA, "gas"],
    })
    out = registry.apply_overrides(reg, ov).set_index("plant_id")
    assert out.loc["mastr:U0", "retirement_date"].year == 2030      # closure applied
    assert out.loc["mastr:U0", "capacity_mw"] == 900.0              # untouched field preserved
    assert out.loc["mastr:U1", "capacity_mw"] == 450.0              # uprate applied
    assert out.loc["mastr:U1", "chp_flag"]                          # observed truth survives override
    assert "workbook:NEW1" in out.index                             # new build appended
    assert len(out) == 4


def test_active_respects_commissioning_and_retirement():
    reg = registry.normalise(_src()).assign(
        retirement_date=pd.to_datetime(["2030-12-31", pd.NA, pd.NA], utc=True))
    assert len(registry.active(reg, 2015)) == 2           # solar (2019) not yet built
    assert len(registry.active(reg, 2025)) == 3
    assert len(registry.active(reg, 2035)) == 2           # lignite retired
