"""Tests for publishing the routine catalogue to a file (ADR-005)."""

import json

from autonomon.routines import nomon_manifest
from autonomon.routines.publish import build_catalog, main, publish


def test_build_catalog_includes_manifest_and_bin():
    catalog = build_catalog()
    assert catalog["name"] == "autonomon"
    assert catalog["routines"] == list(nomon_manifest["routines"])
    assert catalog["params_schema"] == dict(nomon_manifest["params_schema"])
    # The CLI path nomothetic execs to launch a routine, resolved to this venv.
    assert catalog["autonomon_bin"].endswith("nomon-autonomon")
    assert catalog["published_at"]


def test_publish_writes_document_and_no_temp_left(tmp_path):
    target = tmp_path / "sub" / "routine_catalog.json"  # parent created on demand
    written = publish(target)
    assert written == target
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["routines"] == list(nomon_manifest["routines"])
    assert "autonomon_bin" in document
    # The atomic write must not leave its temp file behind.
    assert not (target.parent / f"{target.name}.tmp").exists()


def test_publish_overwrites_existing(tmp_path):
    target = tmp_path / "routine_catalog.json"
    target.write_text("stale", encoding="utf-8")
    publish(target)
    assert json.loads(target.read_text(encoding="utf-8"))["name"] == "autonomon"


def test_main_uses_argv_path(tmp_path, capsys):
    target = tmp_path / "routine_catalog.json"
    assert main([str(target)]) == 0
    assert target.exists()
    assert "published" in capsys.readouterr().out


def test_main_falls_back_to_env_path(tmp_path, monkeypatch):
    target = tmp_path / "routine_catalog.json"
    monkeypatch.setenv("NOMON_ROUTINE_CATALOG_PATH", str(target))
    assert main([]) == 0
    assert target.exists()
