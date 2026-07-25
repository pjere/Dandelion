"""FLEX-F0 — scaffold: flag defaults off, reactor-class physics registry, regime-trajectory defaults."""
from __future__ import annotations

from dispatch_model.flexibility import enabled
from dispatch_model.flexibility import reactor_class as rc
from dispatch_model.flexibility import trajectories as tj


class _StubConfig:
    def __init__(self, d):
        self._d = d

    def section(self, key):
        return self._d.get(key, {})


def test_flag_defaults_off():
    assert enabled(_StubConfig({})) is False
    assert enabled(_StubConfig({"flexibility": {"enabled": False}})) is False
    assert enabled(_StubConfig({"flexibility": {"enabled": True}})) is True


def test_config_yaml_ships_off():
    from dispatch_model.config import load_config
    assert enabled(load_config("config.yaml")) is False        # byte-identical default (golden preserved)


def test_reactor_class_by_capacity():
    assert rc.class_name(capacity_mw=900) == "900"
    assert rc.class_name(capacity_mw=1300) == "1300"
    assert rc.class_name(capacity_mw=1450) == "N4"
    assert rc.class_name(capacity_mw=1650) == "EPR"


def test_reactor_class_by_palier_label():
    assert rc.class_name("CP1") == "900"
    assert rc.class_name("P4") == "1300"
    assert rc.class_name("N4") == "N4"
    assert rc.class_name("EPR2") == "EPR2"
    assert rc.class_name("wat") == "1300"                      # unknown → conservative fallback


def test_physics_are_well_formed():
    for name, p in rc.all_classes().items():
        assert 0 < p["alpha_tech"] < p["alpha_band"] < 1, name    # technical min below band floor
        assert p["d_max_8h"] < p["d_max_day"]                     # 8h budget below daily budget
        assert 0 < p["r_up"] and 0 < p["r_down"] and p["xenon_beta"] > 0


def test_trajectory_defaults_when_workbook_absent():
    fc = tj.load_flex_costs("/no/such/workbook.xlsx", 2024)
    assert fc["c_mod"] == 8.0 and fc["c_start_900"] == 300.0
    assert tj.minstab_mw("/no/such/workbook.xlsx", "FR", 2024) == 0.0
    res = tj.load_reserves("/no/such/workbook.xlsx", 2030)
    assert res["r_up_req"] == 1500.0 and res["r_down_req"] == 1000.0
    lad = tj.load_oa_ladder("/no/such/workbook.xlsx", 2024, price_floor=-500.0, price_cap=4000.0)
    assert lad["oa_bid"] == -500.0 and lad["cr_bid"] == -1.0 and lad["market_cap"] == 4000.0
