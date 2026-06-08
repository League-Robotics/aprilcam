"""Tests for aprilcam.camera.registry — record schema + atomic persistence."""

from __future__ import annotations

import json

import pytest

from aprilcam.camera.registry import (
    REGISTRY_FILENAME,
    CameraRecord,
    CameraRegistry,
)


def _record(uid="avf:0xABC", **kw):
    base = dict(
        unique_id=uid,
        enum=1,
        dir="arducam-ov9782-usb-camera",
        name="Arducam OV9782 USB Camera",
        vid=3141,
        pid=25446,
        serial=None,
        location="0x21141400c456366",
        last_seen="2026-06-07T18:00:00+00:00",
    )
    base.update(kw)
    return CameraRecord(**base)


def test_record_round_trip_dict():
    rec = _record()
    assert CameraRecord.from_dict(rec.to_dict()) == rec


def test_from_dict_requires_unique_id():
    with pytest.raises(ValueError):
        CameraRecord.from_dict({"name": "no id"})


def test_from_dict_ignores_unknown_fields():
    rec = CameraRecord.from_dict({"unique_id": "x", "bogus": 1, "name": "n"})
    assert rec.unique_id == "x" and rec.name == "n"


def test_empty_registry_when_file_absent(tmp_path):
    reg = CameraRegistry(tmp_path)
    assert len(reg) == 0
    assert list(reg.records()) == []
    assert not (tmp_path / REGISTRY_FILENAME).exists()


def test_upsert_then_reload_round_trips(tmp_path):
    reg = CameraRegistry(tmp_path)
    rec = _record()
    reg.upsert(rec)

    # File exists, no .tmp left behind.
    reg_file = tmp_path / REGISTRY_FILENAME
    assert reg_file.exists()
    assert not (tmp_path / (REGISTRY_FILENAME + ".tmp")).exists()

    # Fresh registry reloads the same record.
    reg2 = CameraRegistry(tmp_path)
    assert len(reg2) == 1
    assert reg2.get(rec.unique_id) == rec


def test_upsert_replaces_existing_by_unique_id(tmp_path):
    reg = CameraRegistry(tmp_path)
    reg.upsert(_record(name="old name"))
    reg.upsert(_record(name="new name"))
    assert len(reg) == 1
    assert reg.get("avf:0xABC").name == "new name"


def test_two_distinct_ids_coexist(tmp_path):
    reg = CameraRegistry(tmp_path)
    reg.upsert(_record(uid="avf:0xAAAA", name="Arducam OV9782 USB Camera"))
    reg.upsert(_record(uid="avf:0xBBBB", name="Arducam OV9782 USB Camera"))
    reg2 = CameraRegistry(tmp_path)
    assert len(reg2) == 2
    assert {r.unique_id for r in reg2.records()} == {"avf:0xAAAA", "avf:0xBBBB"}


def test_save_is_atomic_no_tmp_left(tmp_path):
    reg = CameraRegistry(tmp_path)
    reg.upsert(_record(), save=False)
    reg.save()
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == []


def test_on_disk_format_is_versioned_index(tmp_path):
    reg = CameraRegistry(tmp_path)
    reg.upsert(_record())
    data = json.loads((tmp_path / REGISTRY_FILENAME).read_text())
    assert data["version"] == 1
    assert "avf:0xABC" in data["cameras"]
    assert data["cameras"]["avf:0xABC"]["enum"] == 1


def test_corrupt_registry_loads_empty(tmp_path):
    (tmp_path / REGISTRY_FILENAME).write_text("{ not valid json ")
    reg = CameraRegistry(tmp_path)
    assert len(reg) == 0


def test_upsert_without_unique_id_raises(tmp_path):
    reg = CameraRegistry(tmp_path)
    with pytest.raises(ValueError):
        reg.upsert(CameraRecord(unique_id=""))
