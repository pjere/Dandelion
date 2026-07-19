"""Market-rule resolution: the regulatory price floors that are facts, not fitted parameters.

Negative prices were prohibited in IT-North until the TIDE reform (Jan-2025) and in ES until Dec-2023.
Both floors are gone over the projection horizon — history and future run under *different* rules, so
this must live in the model, not in a step-(vii) markup fitted on floored history.
"""
from __future__ import annotations

from dispatch_model.config import load_config
from dispatch_model.rules import DEFAULT_PRICE_FLOOR, DEFAULT_RES_BID, rules_at

ZONES = ["FR", "DE_LU", "IT_NORTH", "ES", "DE_REST"]


def _wb():
    cfg = load_config("config.yaml")
    return cfg.resolve(cfg.section("assumptions")["workbook"])


def test_it_es_floored_at_zero_in_2019():
    bid, floor = rules_at(_wb(), "2019-06-01", ZONES)
    for z in ("IT_NORTH", "ES"):
        assert bid[z] == 0.0 and floor[z] == 0.0        # negative prices prohibited ⇒ hard floor at 0


def test_es_frees_in_dec_2023_it_at_tide_jan_2025():
    _, f_2023 = rules_at(_wb(), "2023-06-01", ZONES)
    _, f_2024 = rules_at(_wb(), "2024-06-01", ZONES)
    _, f_2025 = rules_at(_wb(), "2025-06-01", ZONES)
    assert f_2023["ES"] == 0.0 and f_2024["ES"] < 0.0    # ES: permitted from Dec-2023
    assert f_2024["IT_NORTH"] == 0.0                     # IT: still prohibited in 2024
    assert f_2025["IT_NORTH"] < 0.0                      # IT: TIDE reform, Jan-2025


def test_projection_horizon_has_no_floor_anywhere():
    """The 2027-46 horizon runs under post-reform rules — this is why the floor can't be a fitted markup."""
    bid, floor = rules_at(_wb(), "2030-06-01", ZONES)
    for z in ZONES:
        assert bid[z] == DEFAULT_RES_BID and floor[z] == DEFAULT_PRICE_FLOOR


def test_unlisted_zones_take_the_default():
    bid, floor = rules_at(_wb(), "2019-06-01", ZONES)
    for z in ("FR", "DE_LU", "DE_REST"):                 # DE_REST is virtual and has no rule row
        assert bid[z] == DEFAULT_RES_BID and floor[z] == DEFAULT_PRICE_FLOOR


def test_missing_workbook_falls_back_to_defaults():
    bid, floor = rules_at("", "2019-06-01", ZONES)
    assert set(bid) == set(ZONES)
    assert all(v == DEFAULT_RES_BID for v in bid.values())
    assert all(v == DEFAULT_PRICE_FLOOR for v in floor.values())
