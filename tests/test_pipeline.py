"""Tests hors-ligne du pipeline (aucun accès réseau)."""
from __future__ import annotations

import gzip
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from pricemodeling.config import RteResource
from pricemodeling.db import ensure_rte_table, get_engine, init_db, upsert_df
from pricemodeling.meteo.synop import months_between, parse_month
from pricemodeling.rte.client import chunk_key, iter_chunks
from pricemodeling.rte.extract import normalize


# --------------------------------------------------------------------------- #
#  Découpage temporel
# --------------------------------------------------------------------------- #
def test_months_between():
    assert months_between(date(2014, 11, 1), date(2015, 2, 28)) == [
        "201411", "201412", "201501", "201502"
    ]


def test_iter_chunks_covers_period_without_gap():
    chunks = list(iter_chunks(date(2015, 1, 1), date(2015, 1, 20), chunk_days=7))
    # chaînage continu : fin d'un chunk == début du suivant
    for (_, c1), (c2, _) in zip(chunks, chunks[1:]):
        assert c1 == c2
    assert chunks[0][0].date() == date(2015, 1, 1)
    assert chunks[-1][1].date() == date(2015, 1, 21)  # borne exclusive (end + 1 j)


def test_chunk_key_format():
    chunks = list(iter_chunks(date(2015, 1, 1), date(2015, 1, 7), chunk_days=7))
    assert chunk_key(*chunks[0]) == "2015-01-01_2015-01-08"


# --------------------------------------------------------------------------- #
#  Parsing SYNOP
# --------------------------------------------------------------------------- #
SAMPLE_SYNOP = (
    "numer_sta;date;t;td;u;ff;dd;pmer;rr3\n"
    "07149;20150101000000;283.15;278.15;80;3.5;180;101500;0.2\n"
    "07149;20150101030000;mq;mq;mq;4.0;200;101000;mq\n"
    "07150;20150101000000;281.65;mq;75;2.0;90;mq;1.0\n"
)


def _write_sample(path: Path) -> Path:
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(SAMPLE_SYNOP)
    return path


def test_parse_month_units_and_long_shape(tmp_path):
    gzpath = _write_sample(tmp_path / "synop.201501.csv.gz")
    params = {"t": "temperature_c", "td": "dew_point_c", "u": "humidity_pct",
              "pmer": "pressure_sea_hpa", "rr3": "precip_3h_mm"}
    df = parse_month(gzpath, params)
    # conversion K -> °C
    t = df[(df.station_id == "07149") & (df.ts_utc.str.endswith("00:00:00+00:00")) &
           (df.variable == "temperature_c")]["value"]
    assert abs(float(t.iloc[0]) - 10.0) < 1e-6
    # conversion Pa -> hPa
    p = df[(df.station_id == "07149") & (df.variable == "pressure_sea_hpa")]["value"].iloc[0]
    assert abs(float(p) - 1015.0) < 1e-6
    # 'mq' -> valeur absente (pas de ligne)
    assert df[(df.station_id == "07149") & (df.variable == "temperature_c")].shape[0] == 1
    # forme longue
    assert set(df.columns) == {"station_id", "ts_utc", "variable", "value"}


def test_parse_month_station_filter(tmp_path):
    gzpath = _write_sample(tmp_path / "synop.201501.csv.gz")
    df = parse_month(gzpath, {"u": "humidity_pct"}, stations=["07150"])
    assert set(df.station_id.unique()) == {"07150"}


# --------------------------------------------------------------------------- #
#  Normalisation RTE
# --------------------------------------------------------------------------- #
def test_normalize_per_type():
    res = RteResource(
        name="generation_per_type", api="actual_generation", version="v1",
        resource="x", table="t", chunk_days=155, start_date=date(2014, 12, 15),
    )
    payload = {
        "actual_generations_per_production_type": [
            {"production_type": "NUCLEAR", "values": [
                {"start_date": "2015-01-01T00:00:00+01:00", "end_date": "2015-01-01T01:00:00+01:00", "value": 50000},
                {"start_date": "2015-01-01T01:00:00+01:00", "end_date": "2015-01-01T02:00:00+01:00", "value": 49000},
            ]},
            {"production_type": "WIND", "values": [
                {"start_date": "2015-01-01T00:00:00+01:00", "end_date": "2015-01-01T01:00:00+01:00", "value": 3000},
            ]},
        ]
    }
    df = normalize(res, payload)
    assert len(df) == 3
    # offset +01:00 converti en UTC (00:00 local -> 23:00 UTC veille)
    nuc = df[df.series_key == "NUCLEAR"].sort_values("ts_utc")
    assert nuc.iloc[0]["ts_utc"] == "2014-12-31T23:00:00+00:00"
    assert nuc.iloc[0]["value"] == 50000


def test_normalize_per_unit_keeps_eic_and_name():
    res = RteResource(
        name="generation_per_unit", api="actual_generation", version="v1",
        resource="x", table="t", chunk_days=7, start_date=date(2014, 12, 15),
    )
    payload = {
        "actual_generations_per_unit": [
            {"unit": {"eic_code": "17W100P100P0698Q", "name": "CHOOZ B 1", "production_type": "NUCLEAR"},
             "values": [{"start_date": "2015-01-01T00:00:00+01:00", "end_date": "2015-01-01T01:00:00+01:00", "value": 1400}]},
        ]
    }
    df = normalize(res, payload)
    assert df.iloc[0]["series_key"] == "17W100P100P0698Q"
    assert df.iloc[0]["label"] == "CHOOZ B 1"
    assert df.iloc[0]["sub_key"] == "NUCLEAR"


# --------------------------------------------------------------------------- #
#  Réconciliation
# --------------------------------------------------------------------------- #
def _seed_per_unit(engine):
    ensure_rte_table(engine, "rte_generation_per_unit")
    rows = [
        # même EIC, deux libellés légèrement différents selon l'année
        {"ts_utc": "2015-01-01T00:00:00+00:00", "ts_end_utc": "", "series_key": "17W100P100P0698Q",
         "sub_key": "NUCLEAR", "label": "CHOOZ B 1", "value": 1400},
        {"ts_utc": "2021-01-01T00:00:00+00:00", "ts_end_utc": "", "series_key": "17W100P100P0698Q",
         "sub_key": "NUCLEAR", "label": "CHOOZ B1", "value": 1450},
        # entrée sans EIC fiable, libellé proche -> doit fuzzy-matcher
        {"ts_utc": "2016-01-01T00:00:00+00:00", "ts_end_utc": "", "series_key": "CHOOZ B-1",
         "sub_key": "NUCLEAR", "label": "CHOOZ B-1", "value": 1410},
    ]
    upsert_df(engine, "rte_generation_per_unit", pd.DataFrame(rows), ["ts_utc", "series_key", "sub_key"])


def test_reconcile_units(tmp_path):
    engine = get_engine(f"sqlite:///{(tmp_path/'t.db').as_posix()}")
    init_db(engine)
    _seed_per_unit(engine)
    from pricemodeling.reconcile.units import reconcile_units

    stats = reconcile_units(engine, tmp_path / "report.csv")
    assert stats["units"] == 2          # 1 vrai EIC + 1 entrée sans EIC
    dim = pd.read_sql("SELECT * FROM dim_production_unit", engine)
    real = dim[dim.eic_code == "17W100P100P0698Q"].iloc[0]
    aliases = set(__import__("json").loads(real["aliases"]))
    assert {"CHOOZ B 1", "CHOOZ B1"} <= aliases
    # l'entrée sans EIC doit pointer (canonical_eic) vers le vrai EIC via fuzzy
    noeic = dim[dim.eic_code == "CHOOZ B-1"].iloc[0]
    assert noeic["canonical_eic"] == "17W100P100P0698Q"
    assert noeic["match_source"].startswith("fuzzy")


# --------------------------------------------------------------------------- #
#  Fusion / grille horaire
# --------------------------------------------------------------------------- #
def test_build_master_grid_continuity(tmp_path):
    engine = get_engine(f"sqlite:///{(tmp_path/'t.db').as_posix()}")
    init_db(engine)
    ensure_rte_table(engine, "rte_generation_per_type")
    rows = [
        {"ts_utc": "2015-06-01T00:00:00+00:00", "ts_end_utc": "", "series_key": "NUCLEAR",
         "sub_key": "", "label": "", "value": 50000},
        {"ts_utc": "2015-06-01T01:00:00+00:00", "ts_end_utc": "", "series_key": "NUCLEAR",
         "sub_key": "", "label": "", "value": 49000},
    ]
    upsert_df(engine, "rte_generation_per_type", pd.DataFrame(rows), ["ts_utc", "series_key", "sub_key"])
    from pricemodeling.merge.build_master import build_master

    stats = build_master(engine, date(2015, 6, 1), date(2015, 6, 1), include_units=False)
    assert stats["master_rows"] == 24      # grille horaire continue d'une journée
    master = pd.read_sql("SELECT * FROM master_hourly ORDER BY ts_utc", engine)
    assert "prod_nuclear" in master.columns
    assert master["prod_nuclear"].iloc[0] == 50000
    # heure d'été Paris = UTC+2
    assert int(master["utc_offset_h"].iloc[0]) == 2


def test_master_includes_station_and_unit_columns(tmp_path):
    """master_hourly contient bien les colonnes par station (meteo_<station>_*) et par
    groupe (unit_<groupe>), avec interpolation météo 3h->1h."""
    engine = get_engine(f"sqlite:///{(tmp_path/'t.db').as_posix()}")
    init_db(engine)
    # météo : 2 stations
    meteo = [
        {"station_id": "07149", "ts_utc": "2015-06-01T00:00:00+00:00", "variable": "temperature_c", "value": 10.0},
        {"station_id": "07149", "ts_utc": "2015-06-01T03:00:00+00:00", "variable": "temperature_c", "value": 13.0},
        {"station_id": "07150", "ts_utc": "2015-06-01T00:00:00+00:00", "variable": "wind_speed_ms", "value": 5.0},
    ]
    upsert_df(engine, "synop_obs", pd.DataFrame(meteo), ["station_id", "ts_utc", "variable"])
    # production par groupe : 1 groupe avec EIC + nom
    ensure_rte_table(engine, "rte_generation_per_unit")
    unit = [{"ts_utc": "2015-06-01T00:00:00+00:00", "ts_end_utc": "", "series_key": "17W100P100P0698Q",
             "sub_key": "NUCLEAR", "label": "CHOOZ B 1", "value": 1400.0}]
    upsert_df(engine, "rte_generation_per_unit", pd.DataFrame(unit), ["ts_utc", "series_key", "sub_key"])
    from pricemodeling.reconcile.units import reconcile_units
    reconcile_units(engine, tmp_path / "rep.csv")
    from pricemodeling.merge.build_master import build_master

    build_master(engine, date(2015, 6, 1), date(2015, 6, 1))
    df = pd.read_sql("SELECT * FROM master_hourly ORDER BY ts_utc", engine)
    # colonnes par station
    assert "meteo_07149_temperature_c" in df.columns
    assert "meteo_07150_wind_speed_ms" in df.columns
    assert abs(df["meteo_07149_temperature_c"].iloc[1] - 11.0) < 1e-4   # interpolé à 01h
    # colonne par groupe de production
    assert "unit_chooz_b_1" in df.columns
    assert df["unit_chooz_b_1"].iloc[0] == 1400.0


def test_build_master_dst_fall_back(tmp_path):
    """Passage heure d'hiver (25/10/2015) : grille UTC = 24 h, l'heure locale 02:00 apparaît
    deux fois (offset +02:00 puis +01:00). C'est le comportement robuste attendu d'un index UTC."""
    engine = get_engine(f"sqlite:///{(tmp_path/'t.db').as_posix()}")
    init_db(engine)
    from pricemodeling.merge.build_master import build_master

    stats = build_master(engine, date(2015, 10, 25), date(2015, 10, 25), include_units=False)
    assert stats["master_rows"] == 24      # index UTC : 24 h, sans ambiguïté ni trou
    master = pd.read_sql("SELECT * FROM master_hourly ORDER BY ts_utc", engine)
    # l'offset bascule de +2 (CEST) à +1 (CET) dans la journée
    assert set(master["utc_offset_h"].astype(int)) == {1, 2}
    # l'heure locale "02:00" est présente deux fois (recul d'une heure)
    local_hours = master["ts_local"].str.slice(11, 16)
    assert (local_hours == "02:00").sum() == 2


# --------------------------------------------------------------------------- #
#  ENTSO-E (parsing des prix day-ahead)
# --------------------------------------------------------------------------- #
ENTSOE_A44 = """<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:0">
  <TimeSeries>
    <Period>
      <timeInterval><start>2015-01-01T23:00Z</start><end>2015-01-02T23:00Z</end></timeInterval>
      <resolution>PT60M</resolution>
      <Point><position>1</position><price.amount>25.5</price.amount></Point>
      <Point><position>2</position><price.amount>24.0</price.amount></Point>
      <Point><position>3</position><price.amount>23.1</price.amount></Point>
    </Period>
  </TimeSeries>
</Publication_MarketDocument>"""

ENTSOE_ACK = """<?xml version="1.0" encoding="UTF-8"?>
<Acknowledgement_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-1:acknowledgementdocument:8:0">
  <Reason><code>999</code><text>No matching data found</text></Reason>
</Acknowledgement_MarketDocument>"""


def test_entsoe_parse_prices_timestamps_and_values():
    from pricemodeling.entsoe.prices import parse_prices

    df = parse_prices(ENTSOE_A44, "10YFR-RTE------C").sort_values("ts_utc").reset_index(drop=True)
    assert len(df) == 3
    assert df.loc[0, "ts_utc"] == "2015-01-01T23:00:00+00:00"
    assert df.loc[1, "ts_utc"] == "2015-01-02T00:00:00+00:00"   # PT60M -> +1h
    assert df.loc[0, "value"] == 25.5
    assert df.loc[0, "series_key"] == "10YFR-RTE------C"
    assert df.loc[0, "label"] == "day_ahead_eur_mwh"


def test_entsoe_acknowledgement_is_empty():
    from pricemodeling.entsoe.prices import parse_prices

    df = parse_prices(ENTSOE_ACK, "10YFR-RTE------C")
    assert df.empty


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
