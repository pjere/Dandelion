"""Resumable MaStR download — the truncation trap.

Regression for a real failure: a dropped stream left a **1.72 GB file with a valid `PK` header** that
looked downloaded but was only 56 % of the 3.08 GB export. Size alone and header alone both say "fine";
only reading the central directory (at the *end* of a zip) catches it.
"""
from __future__ import annotations

import zipfile

from pricemodeling.registries.download import export_url, verify_zip


def _good_zip(p):
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("AnlagenEegBiomasse.xml", "<x/>" * 5000)
    return p


def test_verify_accepts_a_complete_zip(tmp_path):
    assert verify_zip(_good_zip(tmp_path / "ok.zip"))


def test_verify_rejects_a_truncated_zip_despite_valid_header(tmp_path):
    p = _good_zip(tmp_path / "trunc.zip")
    data = p.read_bytes()
    p.write_bytes(data[: len(data) // 2])              # keeps the PK header, loses the directory
    assert p.read_bytes()[:2] == b"PK"                 # header still looks valid …
    assert not verify_zip(p)                           # … but verification catches it


def test_verify_rejects_non_zip_and_missing(tmp_path):
    p = tmp_path / "html.zip"
    p.write_bytes(b"<html>error</html>")
    assert not verify_zip(p)
    assert not verify_zip(tmp_path / "nope.zip")


def test_export_url_shape():
    assert export_url("20260715") == (
        "https://download.marktstammdatenregister.de/Gesamtdatenexport_20260715_26.1.zip")
