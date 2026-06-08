"""Tests for aprilcam.camera.registry — record schema + atomic persistence."""

from __future__ import annotations

import json

import pytest

from aprilcam.camera.identity import CameraIdentity
from aprilcam.camera.registry import (
    REGISTRY_FILENAME,
    CameraRecord,
    CameraRegistry,
)


def _identity(uid, name="Arducam OV9782 USB Camera", **kw):
    base = dict(
        unique_id=uid,
        reason="avfoundation_unique_id",
        is_fallback=False,
        name=name,
    )
    base.update(kw)
    return CameraIdentity(**base)


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


# -- enumeration assignment (ticket 011-002) --------------------------------


def test_first_sight_assigns_monotonic_enum(tmp_path):
    reg = CameraRegistry(tmp_path)
    a = reg.resolve(_identity("avf:0xA", name="Cam A"))
    b = reg.resolve(_identity("avf:0xB", name="Cam B"))
    assert a.enum == 1
    assert b.enum == 2
    assert reg.next_enum == 3


def test_next_enum_persists_across_reload(tmp_path):
    reg = CameraRegistry(tmp_path)
    reg.resolve(_identity("avf:0xA", name="Cam A"))
    reg.resolve(_identity("avf:0xB", name="Cam B"))

    data = json.loads((tmp_path / REGISTRY_FILENAME).read_text())
    assert data["next_enum"] == 3

    reg2 = CameraRegistry(tmp_path)
    assert reg2.next_enum == 3
    c = reg2.resolve(_identity("avf:0xC", name="Cam C"))
    assert c.enum == 3


def test_resolve_requires_unique_id(tmp_path):
    reg = CameraRegistry(tmp_path)
    with pytest.raises(ValueError):
        reg.resolve(_identity(""))


# -- reconnect reuse (ticket 011-002) ---------------------------------------


def test_resolve_known_camera_reuses_record(tmp_path):
    reg = CameraRegistry(tmp_path)
    first = reg.resolve(_identity("avf:0xA", name="Cam A"))
    again = reg.resolve(_identity("avf:0xA", name="Cam A"))
    assert again.enum == first.enum
    assert again.dir == first.dir
    assert len(reg) == 1
    assert reg.next_enum == 2  # no new number burned


def test_unplug_replug_reuses_enum_and_dir(tmp_path):
    reg = CameraRegistry(tmp_path)
    first = reg.resolve(_identity("avf:0xA", name="Cam A"))
    enum, cam_dir = first.enum, first.dir

    # Simulate daemon restart / replug: fresh registry from disk, resolve again.
    reg2 = CameraRegistry(tmp_path)
    again = reg2.resolve(_identity("avf:0xA", name="Cam A"))
    assert again.enum == enum
    assert again.dir == cam_dir
    assert len(reg2) == 1
    assert reg2.next_enum == 2


def test_resolve_refreshes_identity_fields(tmp_path):
    reg = CameraRegistry(tmp_path)
    reg.resolve(_identity("avf:0xA", name="Cam A", location=None))
    updated = reg.resolve(_identity("avf:0xA", name="Cam A", location="0xPORT"))
    assert updated.location == "0xPORT"
    assert updated.last_seen is not None


# -- identical-model disambiguation (ticket 011-002) ------------------------


def test_two_identical_model_cameras_get_distinct_dirs(tmp_path):
    reg = CameraRegistry(tmp_path)
    a = reg.resolve(_identity("avf:0xA", name="Arducam OV9782 USB Camera"))
    b = reg.resolve(_identity("avf:0xB", name="Arducam OV9782 USB Camera"))

    assert a.enum != b.enum
    assert a.dir == "arducam-ov9782-usb-camera"  # first keeps the bare slug
    assert b.dir != a.dir  # second disambiguated
    assert b.dir.startswith("arducam-ov9782-usb-camera-")
    assert b.dir == f"arducam-ov9782-usb-camera-{b.enum}"


def test_identical_model_preserves_first_dir_data(tmp_path):
    # First camera already has a populated slug dir.
    slug = "arducam-ov9782-usb-camera"
    (tmp_path / slug).mkdir()
    (tmp_path / slug / "calibration.json").write_text('{"keep": true}')

    reg = CameraRegistry(tmp_path)
    # Adoption created a placeholder record owning the legacy slug dir; the two
    # real cameras that resolve to that slug each get fresh, distinct dirs and
    # never steal or rename the populated legacy dir.
    a = reg.resolve(_identity("avf:0xA", name="Arducam OV9782 USB Camera"))
    b = reg.resolve(_identity("avf:0xB", name="Arducam OV9782 USB Camera"))

    assert a.dir != b.dir
    assert a.dir != slug and b.dir != slug  # legacy dir untouched
    # The pre-existing data dir is still there with its original contents.
    assert (tmp_path / slug / "calibration.json").read_text() == '{"keep": true}'


# -- data-dir adoption / migration (ticket 011-002) -------------------------


def test_adopts_legacy_dir_without_renaming(tmp_path):
    slug = "hd-usb-camera"
    legacy = tmp_path / slug
    legacy.mkdir()
    (legacy / "calibration.json").write_text('{"homography": [1, 2, 3]}')
    (legacy / "paths.json").write_text("[]")
    (legacy / "info.json").write_text('{"name": "HD USB Camera"}')

    reg = CameraRegistry(tmp_path)

    # A record now owns the slug dir, and nothing was renamed.
    dirs = {r.dir for r in reg.records()}
    assert slug in dirs
    assert legacy.is_dir()
    assert (legacy / "calibration.json").read_text() == '{"homography": [1, 2, 3]}'
    assert (legacy / "paths.json").exists()
    assert (legacy / "info.json").exists()


def test_adoption_is_idempotent(tmp_path):
    slug = "hd-usb-camera"
    (tmp_path / slug).mkdir()
    (tmp_path / slug / "calibration.json").write_text("{}")

    reg = CameraRegistry(tmp_path)
    n_first = len(reg)
    added = reg.adopt_existing_dirs()
    assert added == 0
    assert len(reg) == n_first

    # Reload from disk: still no duplicate record for the slug.
    reg2 = CameraRegistry(tmp_path)
    assert len([r for r in reg2.records() if r.dir == slug]) == 1


def test_adoption_skips_dirs_already_owned_by_records(tmp_path):
    # A resolved camera owns its slug dir; adoption must not create a second
    # record for the same dir.
    reg = CameraRegistry(tmp_path)
    rec = reg.resolve(_identity("avf:0xA", name="HD USB Camera"))
    (tmp_path / rec.dir).mkdir(exist_ok=True)

    reg2 = CameraRegistry(tmp_path)
    matching = [r for r in reg2.records() if r.dir == rec.dir]
    assert len(matching) == 1
    assert matching[0].unique_id == "avf:0xA"


def test_registry_json_skipped_during_adoption(tmp_path):
    # The registry.json file is not a dir, so it is never adopted as a camera.
    reg = CameraRegistry(tmp_path)
    reg.resolve(_identity("avf:0xA", name="Cam A"))
    reg2 = CameraRegistry(tmp_path)
    assert all(not (r.dir or "").endswith(".json") for r in reg2.records())
