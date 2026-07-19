"""REMIT unavailability (#41): defensive parse, cancelled-drop, planned/forced split, daily reconstruction.

Uses a synthetic entsoe-py-shaped frame + an on-disk SQLite so no network/token is needed."""
from __future__ import annotations

import pandas as pd
import pytest

from pricemodeling.db import get_engine, init_db
from pricemodeling.entsoe.unavailability import (
    _outage_type,
    _parse_unavailability,
    ensure_unavailability_table,
    nuclear_available_fraction,
    outage_rate_summary,
    reconstruct_daily_availability,
)


def test_outage_type_text_labels_and_codes():
    # regression: "Unplanned outage" contains the substring "PLANNED" — must NOT be read as planned
    assert _outage_type("Unplanned outage") == "forced"
    assert _outage_type("Planned maintenance") == "planned"
    assert _outage_type("A53") == "planned"
    assert _outage_type("A54") == "forced"
    assert _outage_type("") == "forced"


def _raw():
    """Mimic entsoe-py query_unavailability_of_generation_units output (column names it actually emits)."""
    return pd.DataFrame([
        # planned full outage of a 900 MW unit, 10 days
        {"mrid": "M1", "businesstype": "A53", "docstatus": "A05", "production_resource_id": "EIC_A",
         "production_resource_name": "NUKE 1", "plant_type": "B14", "nominal_power": 900.0,
         "avail_qty": 0.0, "start": "2023-03-01T00:00:00Z", "end": "2023-03-11T00:00:00Z",
         "created_doc_time": "2023-02-01T00:00:00Z"},
        # forced PARTIAL derate of the same unit (900→400 available => 500 unavailable), overlaps M1
        {"mrid": "M2", "businesstype": "A54", "docstatus": "A05", "production_resource_id": "EIC_A",
         "production_resource_name": "NUKE 1", "plant_type": "B14", "nominal_power": 900.0,
         "avail_qty": 400.0, "start": "2023-03-05T00:00:00Z", "end": "2023-03-08T00:00:00Z",
         "created_doc_time": "2023-03-04T00:00:00Z"},
        # forced outage of a second unit
        {"mrid": "M3", "businesstype": "A54", "docstatus": "A05", "production_resource_id": "EIC_B",
         "production_resource_name": "COAL 2", "plant_type": "B02", "nominal_power": 600.0,
         "avail_qty": 0.0, "start": "2023-07-01T00:00:00Z", "end": "2023-07-06T00:00:00Z",
         "created_doc_time": "2023-06-20T00:00:00Z"},
        # CANCELLED message — must be dropped
        {"mrid": "M4", "businesstype": "A53", "docstatus": "A09", "production_resource_id": "EIC_B",
         "production_resource_name": "COAL 2", "plant_type": "B02", "nominal_power": 600.0,
         "avail_qty": 0.0, "start": "2023-08-01T00:00:00Z", "end": "2023-08-10T00:00:00Z",
         "created_doc_time": "2023-07-15T00:00:00Z"},
    ])


def test_parse_maps_type_computes_unavailable_and_drops_cancelled():
    df = _parse_unavailability(_raw(), "FR")
    assert set(df["mrid"]) == {"M1", "M2", "M3"}           # M4 (A09 cancelled) dropped
    a = df[df["mrid"] == "M1"].iloc[0]
    assert a["outage_type"] == "planned" and a["unavailable_mw"] == 900.0
    p = df[df["mrid"] == "M2"].iloc[0]
    assert p["outage_type"] == "forced" and p["unavailable_mw"] == 500.0   # partial derate captured
    assert df["start_utc"].str.endswith("+00:00").all()


def test_parse_is_defensive_about_column_names():
    # alternate column spellings entsoe-py has used across versions
    alt = pd.DataFrame([{"mRID": "X1", "business_type": "A54", "doc_status": "A05",
                         "eic": "EIC_Z", "unit_name": "GT", "nominal_mw": 100.0, "quantity": 30.0,
                         "start_utc": "2023-01-01T00:00:00Z", "end_utc": "2023-01-03T00:00:00Z"}])
    df = _parse_unavailability(alt, "BE")
    assert len(df) == 1 and df.iloc[0]["unavailable_mw"] == 70.0 and df.iloc[0]["eic"] == "EIC_Z"


def _engine(tmp_path):
    engine = get_engine(f"sqlite:///{(tmp_path/'t.db').as_posix()}")
    init_db(engine)
    ensure_unavailability_table(engine)
    from pricemodeling.db import upsert_df
    upsert_df(engine, "entsoe_unavailability", _parse_unavailability(_raw(), "FR"), ["mrid", "start_utc"])
    return engine


def test_reconstruction_takes_most_restrictive_overlapping_message(tmp_path):
    daily = reconstruct_daily_availability(_engine(tmp_path), "FR", 2023)
    a = daily[daily["eic"] == "EIC_A"].set_index("date")
    # Mar-01..04: only M1 (full 900 out) → available 0; Mar-05..07: M1(900) still more restrictive than M2(500)
    assert a.loc[pd.Timestamp("2023-03-02").date(), "available_mw"] == 0.0
    assert a.loc[pd.Timestamp("2023-03-06").date(), "unavailable_mw"] == 900.0   # max(900,500)
    # outside any message → no row (implicitly fully available)
    assert pd.Timestamp("2023-06-01").date() not in set(a.index)


def test_outage_rate_summary_splits_planned_and_forced_at_message_level(tmp_path):
    s = outage_rate_summary(_engine(tmp_path), "FR", 2023)
    assert s["units_with_outage"] == 2
    # message-level energy (MW·h), so the forced M2 derate overlapping the planned M1 is NOT masked:
    # planned = 900·240h = 216000; forced = 500·72h + 600·120h = 108000; planned share = 216000/324000
    assert s["planned_share"] == pytest.approx(0.667, abs=0.01)
    assert s["forced_share"] == pytest.approx(0.333, abs=0.01)


def test_nuclear_available_fraction(tmp_path):
    # tag the fixture units as Nuclear via a direct edit, then check the daily available fraction
    engine = _engine(tmp_path)
    from sqlalchemy import text
    with engine.begin() as c:
        c.execute(text("UPDATE entsoe_unavailability SET plant_type='Nuclear'"))
    frac = nuclear_available_fraction(engine, "FR", 2023)
    # fleet nominal = 900 (EIC_A) + 600 (EIC_B) = 1500; Mar-02 only M1 out (900) → 1-900/1500 = 0.4
    assert frac.loc[pd.Timestamp("2023-03-02").date()] == pytest.approx(0.4, abs=0.01)
    assert frac.loc[pd.Timestamp("2023-06-01").date()] == pytest.approx(1.0)   # no outage → fully available
    assert (frac >= 0).all() and (frac <= 1).all()


# ---- #80 zone availability stats (Phase-2 review, F-gap closure) -----------------------------------------

def _engine_with_installed(tmp_path):
    """Fixture engine + a minimal entsoe_installed_capacity so the stats denominator exists."""
    from sqlalchemy import text
    engine = _engine(tmp_path)
    with engine.begin() as c:
        c.execute(text("UPDATE entsoe_unavailability SET plant_type='Nuclear' WHERE eic='EIC_A'"))
        c.execute(text("UPDATE entsoe_unavailability SET plant_type='Fossil Hard coal' WHERE eic='EIC_B'"))
        c.execute(text("CREATE TABLE IF NOT EXISTS entsoe_installed_capacity "
                       "(series_key TEXT, sub_key TEXT, label TEXT, value REAL)"))
        c.execute(text("INSERT INTO entsoe_installed_capacity VALUES "
                       "('FR','Nuclear','installed_mw',900.0),"
                       "('FR','Fossil Hard coal','installed_mw',600.0),"
                       "('FR','Fossil Gas','installed_mw',400.0)"))   # gas: installed but no REMIT outage
    return engine


def test_installed_by_tech_reads_latest_positive(tmp_path):
    from pricemodeling.entsoe.unavailability import installed_by_tech
    inst = installed_by_tech(_engine_with_installed(tmp_path), "FR")
    assert inst == {"Nuclear": 900.0, "Fossil Hard coal": 600.0, "Fossil Gas": 400.0}


def test_tech_unavailable_mw_daily_series(tmp_path):
    from pricemodeling.entsoe.unavailability import tech_unavailable_mw
    s = tech_unavailable_mw(_engine_with_installed(tmp_path), "FR", 2023, "Nuclear")
    assert len(s) == 365                                   # full-year daily grid, zeros outside outages
    assert s.loc[pd.Timestamp("2023-03-02").date()] == 900.0
    assert s.loc[pd.Timestamp("2023-06-01").date()] == 0.0


def test_zone_availability_stats_mean_and_masking(tmp_path):
    from pricemodeling.entsoe.unavailability import zone_availability_stats
    stats = zone_availability_stats(_engine_with_installed(tmp_path), "FR", [2023], min_installed_mw=100.0)
    # nuclear: 900 MW out for 10 days of 365 → mean daily unavailable = 900·10/365 ≈ 24.66 MW
    assert stats["Nuclear"]["mean_avail"] == pytest.approx(1 - 900 * 10 / 365 / 900.0, abs=1e-3)
    assert stats["Nuclear"]["n_years"] == 1
    # coal: 600 MW out 5 days → avail ≈ 0.986
    assert stats["Fossil Hard coal"]["mean_avail"] == pytest.approx(1 - 600 * 5 / 365 / 600.0, abs=1e-3)
    # gas has installed capacity but no REMIT outages → availability 1.0, zero spread (a no-op multiplier)
    assert stats["Fossil Gas"]["mean_avail"] == 1.0 and stats["Fossil Gas"]["std_avail"] == 0.0


def test_eic_plant_type_majority_vote(tmp_path):
    # a unit whose label drifts (1× 'Other', 3× 'Fossil Gas') must map to its dominant label
    from sqlalchemy import text

    from pricemodeling.entsoe.unavailability import _eic_plant_type
    engine = _engine(tmp_path)
    with engine.begin() as c:
        for i, pt in enumerate(["Fossil Gas", "Fossil Gas", "Fossil Gas", "Other"]):
            c.execute(text("INSERT INTO entsoe_unavailability (mrid, start_utc, zone, eic, plant_type, "
                           "unavailable_mw) VALUES (:m, :s, 'FR', 'EIC_MIX', :pt, 10.0)"),
                      {"m": f"MX{i}", "s": f"2023-09-0{i + 1}T00:00:00+00:00", "pt": pt})
    assert _eic_plant_type(engine, "FR")["EIC_MIX"] == "Fossil Gas"
