"""Tests for aprilcam.camera.registry — record schema + atomic persistence."""

from __future__ import annotations

import json

import pytest

from aprilcam.camera.identity import CameraIdentity
from aprilcam.camera.registry import (
    REGISTRY_FILENAME,
    CameraRecord,
    CameraRegistry,
    CameraSelectError,
    resolve_enum_to_index,
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
    # A populated slug dir already exists on disk.
    slug = "arducam-ov9782-usb-camera"
    (tmp_path / slug).mkdir()
    (tmp_path / slug / "calibration.json").write_text('{"keep": true}')

    reg = CameraRegistry(tmp_path)
    # The first camera to resolve to that slug ADOPTS the existing dir (reusing
    # its calibration). The second identical-model camera, finding the slug now
    # owned by a live record, is disambiguated and never steals or renames the
    # populated dir.
    a = reg.resolve(_identity("avf:0xA", name="Arducam OV9782 USB Camera"))
    b = reg.resolve(_identity("avf:0xB", name="Arducam OV9782 USB Camera"))

    assert a.dir == slug  # first camera adopts the existing calibrated dir
    assert b.dir != slug and b.dir != a.dir  # second disambiguated
    # The pre-existing data dir is still there with its original contents.
    assert (tmp_path / slug / "calibration.json").read_text() == '{"keep": true}'


# -- connect-time dir adoption (ticket 011-003) -----------------------------


def test_first_seen_camera_adopts_existing_matching_slug_dir(tmp_path):
    # A camera's calibrated dir already exists on disk from a previous life.
    slug = "hd-usb-camera"
    legacy = tmp_path / slug
    legacy.mkdir()
    (legacy / "calibration.json").write_text('{"homography": [1, 2, 3]}')
    (legacy / "paths.json").write_text("[]")
    (legacy / "info.json").write_text('{"name": "HD USB Camera"}')

    reg = CameraRegistry(tmp_path)
    # No phantom record exists merely because the dir is on disk.
    assert len(reg) == 0

    # On first connect the camera adopts the bare slug dir, not <slug>-<enum>.
    rec = reg.resolve(_identity("avf:0xA", name="HD USB Camera"))
    assert rec.dir == slug
    # Nothing was renamed and the calibration is preserved in place.
    assert legacy.is_dir()
    assert (legacy / "calibration.json").read_text() == '{"homography": [1, 2, 3]}'
    assert (legacy / "paths.json").exists()
    assert (legacy / "info.json").exists()


def test_no_records_fabricated_for_never_connected_dirs(tmp_path):
    # Two on-disk dirs that no camera has ever connected for.
    (tmp_path / "hd-usb-camera").mkdir()
    (tmp_path / "hd-usb-camera" / "calibration.json").write_text("{}")
    (tmp_path / "other-camera").mkdir()

    reg = CameraRegistry(tmp_path)
    assert len(reg) == 0
    assert list(reg.records()) == []

    # Reload from disk: still empty, no phantom dir:<slug> records persisted.
    reg2 = CameraRegistry(tmp_path)
    assert len(reg2) == 0


def test_adopt_existing_dirs_is_noop(tmp_path):
    (tmp_path / "hd-usb-camera").mkdir()
    (tmp_path / "hd-usb-camera" / "calibration.json").write_text("{}")

    reg = CameraRegistry(tmp_path)
    added = reg.adopt_existing_dirs()
    assert added == 0
    assert len(reg) == 0


def test_adopt_flag_does_not_fabricate_records(tmp_path):
    # Even with the legacy ``adopt=True`` flag, no records are fabricated.
    (tmp_path / "hd-usb-camera").mkdir()
    reg = CameraRegistry(tmp_path, adopt=True)
    assert len(reg) == 0


def test_resolve_disambiguates_only_against_live_records(tmp_path):
    # No on-disk dir for this slug: first camera takes the bare slug, the
    # identical-model second is disambiguated against the live record.
    reg = CameraRegistry(tmp_path)
    a = reg.resolve(_identity("avf:0xA", name="HD USB Camera"))
    b = reg.resolve(_identity("avf:0xB", name="HD USB Camera"))
    assert a.dir == "hd-usb-camera"
    assert b.dir == f"hd-usb-camera-{b.enum}"


def test_reconnect_reuses_enum_and_dir_after_adoption(tmp_path):
    # First connect adopts an existing slug dir; a later reconnect (fresh
    # registry from disk) reuses the same enum and dir.
    slug = "hd-usb-camera"
    (tmp_path / slug).mkdir()
    reg = CameraRegistry(tmp_path)
    first = reg.resolve(_identity("avf:0xA", name="HD USB Camera"))
    assert first.dir == slug

    reg2 = CameraRegistry(tmp_path)
    again = reg2.resolve(_identity("avf:0xA", name="HD USB Camera"))
    assert again.enum == first.enum
    assert again.dir == slug
    assert len(reg2) == 1


# -- resolve_enum_to_index (enumeration number → live OS index) -------------


def test_resolve_enum_to_index_returns_live_index(tmp_path):
    # A camera registered with enum #1 is currently connected at OS index 3.
    reg = CameraRegistry(tmp_path)
    rec = reg.resolve(_identity("avf:0xA", name="Brio 501"))
    assert rec.enum == 1
    live = {3: _identity("avf:0xA", name="Brio 501")}
    assert resolve_enum_to_index(1, reg, live) == 3


def test_resolve_enum_to_index_unknown_enum_errors(tmp_path):
    reg = CameraRegistry(tmp_path)
    reg.resolve(_identity("avf:0xA", name="Brio 501"))  # enum #1
    live = {0: _identity("avf:0xA", name="Brio 501")}
    with pytest.raises(CameraSelectError) as exc:
        resolve_enum_to_index(5, reg, live)
    assert "no camera #5" in str(exc.value)


def test_resolve_enum_to_index_disconnected_errors(tmp_path):
    # Known enum, but the camera is not in the live identity table.
    reg = CameraRegistry(tmp_path)
    reg.resolve(_identity("avf:0xA", name="Brio 501"))  # enum #1
    live = {}  # nothing connected
    with pytest.raises(CameraSelectError) as exc:
        resolve_enum_to_index(1, reg, live)
    assert "not connected" in str(exc.value)
    assert "#1" in str(exc.value)
