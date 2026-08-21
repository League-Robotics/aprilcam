"""Tests for ``aprilcam.camera.registry`` (headless, no hardware).

Covers ticket 020-002's acceptance criteria: idempotent resolve (AC2),
monotonic never-reshuffling enumeration numbers (AC3), ``forget`` vs.
absent-but-remembered (AC4), atomic-write crash safety (AC5), and
last-resort-slug collision disambiguation (AC6).

Identity fixtures are constructed directly (no hardware, no
``resolve_identity`` call) — this ticket's registry logic is tested
against recorded/constructed identities, per the ticket's testing note.
"""

from __future__ import annotations

import json
import os

import pytest

from aprilcam.camera import identity as identity_module
from aprilcam.camera.identity import CameraIdentity
from aprilcam.camera.interfaces import CameraRegistry as CameraRegistryProtocol
from aprilcam.camera.registry import (
    FIRST_NUMBER,
    REGISTRY_FILENAME,
    REGISTRY_VERSION,
    CameraRecord,
    CameraRegistry,
)
from aprilcam.errors import UnknownCamera


def _identity(hardware_key: str, device_name: str, **kw) -> CameraIdentity:
    base = dict(
        slug=identity_module.device_name_slug(device_name),
        device_name=device_name,
        hardware_key=hardware_key,
        source="vid_pid_location",
        is_fallback=False,
    )
    base.update(kw)
    return CameraIdentity(**base)


# ---------------------------------------------------------------------------
# CameraRecord — schema round trip
# ---------------------------------------------------------------------------


def test_record_round_trip_dict():
    rec = CameraRecord(
        hardware_key="hw:1",
        slug="cam-a",
        number=1,
        device_name="Cam A",
        last_seen="2026-07-31T00:00:00+00:00",
    )
    assert CameraRecord.from_dict(rec.to_dict()) == rec


def test_from_dict_ignores_unknown_fields():
    rec = CameraRecord.from_dict(
        {
            "hardware_key": "hw:1",
            "slug": "cam-a",
            "number": 1,
            "device_name": "Cam A",
            "last_seen": "t",
            "bogus": True,
        }
    )
    assert rec.hardware_key == "hw:1" and rec.slug == "cam-a"


# ---------------------------------------------------------------------------
# Empty / load behaviour
# ---------------------------------------------------------------------------


def test_empty_registry_when_file_absent(tmp_path):
    cameras_dir = tmp_path / "cameras"
    reg = CameraRegistry(cameras_dir)
    assert len(reg) == 0
    assert reg.records() == ()
    # Loading never creates the directory -- only a write does.
    assert not cameras_dir.exists()


def test_corrupt_registry_loads_empty(tmp_path):
    (tmp_path / REGISTRY_FILENAME).write_text("{ not valid json")
    reg = CameraRegistry(tmp_path)
    assert len(reg) == 0


# ---------------------------------------------------------------------------
# resolve() -- idempotent, monotonic enumeration (AC2, AC3)
# ---------------------------------------------------------------------------


def test_first_sight_assigns_monotonic_number(tmp_path):
    reg = CameraRegistry(tmp_path)
    a_slug = reg.resolve(_identity("hw:a", "Cam A"))
    b_slug = reg.resolve(_identity("hw:b", "Cam B"))
    assert reg.get(a_slug).number == FIRST_NUMBER
    assert reg.get(b_slug).number == FIRST_NUMBER + 1


def test_resolve_known_camera_reuses_slug_and_number(tmp_path):
    reg = CameraRegistry(tmp_path)
    first_slug = reg.resolve(_identity("hw:a", "Cam A"))
    first_number = reg.get(first_slug).number

    again_slug = reg.resolve(_identity("hw:a", "Cam A"))

    assert again_slug == first_slug
    assert reg.get(again_slug).number == first_number
    assert len(reg) == 1


def test_resolve_idempotent_across_simulated_restart(tmp_path):
    """UC-01.S-1's restart-stability property: a fresh CameraRegistry
    instance loaded from the same directory resolves the same physical
    camera to the same slug and number."""
    reg = CameraRegistry(tmp_path)
    slug1 = reg.resolve(_identity("hw:a", "Cam A"))
    number1 = reg.get(slug1).number

    reg2 = CameraRegistry(tmp_path)  # simulated daemon restart
    slug2 = reg2.resolve(_identity("hw:a", "Cam A"))
    number2 = reg2.get(slug2).number

    assert slug1 == slug2
    assert number1 == number2


def test_number_never_reshuffles_when_new_camera_added(tmp_path):
    reg = CameraRegistry(tmp_path)
    a_slug = reg.resolve(_identity("hw:a", "Cam A"))
    a_number = reg.get(a_slug).number

    reg2 = CameraRegistry(tmp_path)
    reg2.resolve(_identity("hw:b", "Cam B"))  # a new camera arrives
    a_slug_again = reg2.resolve(_identity("hw:a", "Cam A"))

    assert reg2.get(a_slug_again).number == a_number


def test_reconnect_refreshes_device_name_and_last_seen(tmp_path):
    reg = CameraRegistry(tmp_path)
    slug = reg.resolve(_identity("hw:a", "Cam A"))
    first_seen = reg.get(slug).last_seen

    updated_slug = reg.resolve(_identity("hw:a", "Cam A Renamed"))

    assert updated_slug == slug  # slug itself never changes on reconnect
    updated = reg.get(slug)
    assert updated.device_name == "Cam A Renamed"
    assert updated.last_seen >= first_seen


def test_reconnect_keeps_stored_slug_when_identity_proposes_a_different_one(
    tmp_path,
):
    """UC-25's closed "naming on reconnect" open question, exercised
    directly: the same ``hardware_key`` reconnects with a freshly
    resolved ``CameraIdentity`` whose *own* ``slug`` differs from what
    is already stored -- not just a changed ``device_name`` (which
    ``test_reconnect_refreshes_device_name_and_last_seen`` above already
    covers, and which does not by itself change the derived slug).
    ``resolve()`` must return the stored slug unchanged, never the newly
    proposed one -- this is the "reconnect does not rename" behaviour
    ``camera/registry.py``'s module and ``resolve()`` docstrings
    describe, and that ``docs/design/use-cases/cli.md``'s UC-25 now
    records as resolved rather than open.
    """
    reg = CameraRegistry(tmp_path)
    stored_slug = reg.resolve(_identity("hw:a", "Cam A"))
    assert stored_slug == "cam-a"

    reconnected_slug = reg.resolve(
        _identity("hw:a", "Cam A", slug="totally-different-slug")
    )

    assert reconnected_slug == stored_slug  # stored slug wins, not proposed
    assert reg.get(stored_slug) is not None
    assert reg.get("totally-different-slug") is None
    assert len(reg) == 1  # still one record for this hardware_key


def test_resolve_requires_a_slug(tmp_path):
    reg = CameraRegistry(tmp_path)
    empty_identity = CameraIdentity(
        slug="", device_name="", hardware_key="", source="name_resolution_slug",
        is_fallback=True,
    )
    with pytest.raises(ValueError):
        reg.resolve(empty_identity)


def test_next_number_persists_and_clamps_against_stored_max(tmp_path):
    reg = CameraRegistry(tmp_path)
    reg.resolve(_identity("hw:a", "Cam A"))
    reg.resolve(_identity("hw:b", "Cam B"))
    data = json.loads((tmp_path / REGISTRY_FILENAME).read_text())
    assert data["next_number"] == FIRST_NUMBER + 2

    # Simulate a corrupted/rolled-back counter while the records survive;
    # clamping must still avoid reissuing an in-use number.
    data["next_number"] = FIRST_NUMBER
    (tmp_path / REGISTRY_FILENAME).write_text(json.dumps(data))

    reg2 = CameraRegistry(tmp_path)
    c_slug = reg2.resolve(_identity("hw:c", "Cam C"))
    assert reg2.get(c_slug).number == FIRST_NUMBER + 2


# ---------------------------------------------------------------------------
# get_by_number() -- the number-addressing half of ticket 021-002
# ---------------------------------------------------------------------------


def test_get_by_number_round_trips_against_a_freshly_resolved_record(tmp_path):
    reg = CameraRegistry(tmp_path)
    slug = reg.resolve(_identity("hw:a", "Cam A"))
    record = reg.get(slug)

    assert reg.get_by_number(record.number) == record


def test_get_by_number_returns_none_for_an_unassigned_number(tmp_path):
    reg = CameraRegistry(tmp_path)
    reg.resolve(_identity("hw:a", "Cam A"))

    assert reg.get_by_number(999) is None


def test_get_by_number_finds_the_right_record_among_several(tmp_path):
    reg = CameraRegistry(tmp_path)
    a_slug = reg.resolve(_identity("hw:a", "Cam A"))
    b_slug = reg.resolve(_identity("hw:b", "Cam B"))
    a_record = reg.get(a_slug)
    b_record = reg.get(b_slug)

    assert reg.get_by_number(a_record.number) == a_record
    assert reg.get_by_number(b_record.number) == b_record


def test_get_by_number_survives_a_gap_left_by_forget(tmp_path):
    """Numbers are never reissued or reshuffled -- a gap left by forget
    stays a gap, and get_by_number reports it as absent, not as some
    other camera shifted into its place."""
    reg = CameraRegistry(tmp_path)
    a_slug = reg.resolve(_identity("hw:a", "Cam A"))
    a_number = reg.get(a_slug).number
    b_slug = reg.resolve(_identity("hw:b", "Cam B"))
    b_record = reg.get(b_slug)

    reg.forget(a_slug)

    assert reg.get_by_number(a_number) is None
    assert reg.get_by_number(b_record.number) == b_record


# ---------------------------------------------------------------------------
# forget() vs. absent-but-remembered (AC4)
# ---------------------------------------------------------------------------


def test_camera_not_forgotten_keeps_its_entry(tmp_path):
    """An absent (e.g. unplugged) camera is simply never re-resolved --
    that alone must not remove it. Only forget() removes an entry."""
    reg = CameraRegistry(tmp_path)
    slug = reg.resolve(_identity("hw:a", "Cam A"))

    reg2 = CameraRegistry(tmp_path)  # simulated restart; camera absent
    assert len(reg2) == 1
    assert reg2.get(slug) is not None


def test_forget_removes_entry_outright(tmp_path):
    reg = CameraRegistry(tmp_path)
    slug = reg.resolve(_identity("hw:a", "Cam A"))
    assert reg.get(slug) is not None

    reg.forget(slug)

    assert reg.get(slug) is None
    assert len(reg) == 0

    # And the removal is persisted.
    reg2 = CameraRegistry(tmp_path)
    assert len(reg2) == 0


def test_forget_unknown_slug_raises_unknown_camera(tmp_path):
    reg = CameraRegistry(tmp_path)
    reg.resolve(_identity("hw:a", "Cam A"))

    with pytest.raises(UnknownCamera) as exc:
        reg.forget("does-not-exist")

    assert exc.value.requested == "does-not-exist"
    assert "cam-a" in exc.value.available


def test_forgotten_camera_reappearing_gets_a_new_number(tmp_path):
    """Forgetting is permanent removal, not a pause -- a forgotten camera
    that reconnects is treated as new."""
    reg = CameraRegistry(tmp_path)
    slug = reg.resolve(_identity("hw:a", "Cam A"))
    old_number = reg.get(slug).number
    reg.forget(slug)

    new_slug = reg.resolve(_identity("hw:a", "Cam A"))

    assert reg.get(new_slug).number != old_number


# ---------------------------------------------------------------------------
# Atomic-write crash safety (AC5)
# ---------------------------------------------------------------------------


def test_atomic_write_crash_leaves_prior_state(tmp_path, monkeypatch):
    reg = CameraRegistry(tmp_path)
    reg.resolve(_identity("hw:a", "Cam A"))
    reg_file = tmp_path / REGISTRY_FILENAME
    prior_content = reg_file.read_text()

    def boom(*_a, **_k):
        raise OSError("simulated crash between fsync and rename")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(OSError):
        reg.resolve(_identity("hw:b", "Cam B"))

    # The prior, fully-written file is exactly as it was -- never
    # partially written, never lost.
    assert reg_file.read_text() == prior_content
    assert list(tmp_path.glob("*.tmp")) == []


def test_save_leaves_no_tmp_file_on_success(tmp_path):
    reg = CameraRegistry(tmp_path)
    reg.resolve(_identity("hw:a", "Cam A"))
    assert list(tmp_path.glob("*.tmp")) == []
    assert (tmp_path / REGISTRY_FILENAME).exists()


@pytest.mark.skipif(
    os.name != "posix" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="permission bits are not meaningful for root or on non-POSIX systems",
)
def test_save_wraps_permission_error_with_path(tmp_path):
    parent = tmp_path / "readonly"
    parent.mkdir()
    parent.chmod(0o555)
    try:
        reg = CameraRegistry(parent / "cameras")
        with pytest.raises(OSError, match="cannot create camera registry directory"):
            reg.resolve(_identity("hw:a", "Cam A"))
    finally:
        parent.chmod(0o755)


# ---------------------------------------------------------------------------
# Last-resort slug collision disambiguation, not merge (AC6)
# ---------------------------------------------------------------------------


def test_name_collision_disambiguated_not_merged(tmp_path):
    reg = CameraRegistry(tmp_path)
    a_slug = reg.resolve(
        _identity("vidpid:0c45:6366@loc1", "Arducam OV9782 USB Camera")
    )
    b_slug = reg.resolve(
        _identity("vidpid:0c45:6366@loc2", "Arducam OV9782 USB Camera")
    )

    assert a_slug == "arducam-ov9782-usb-camera"  # first keeps the bare slug
    assert b_slug != a_slug
    assert b_slug.startswith("arducam-ov9782-usb-camera-")
    assert len(reg.records()) == 2  # two distinct entries, never merged
    assert {r.hardware_key for r in reg.records()} == {
        "vidpid:0c45:6366@loc1",
        "vidpid:0c45:6366@loc2",
    }


def test_three_way_collision_each_gets_a_distinct_slug(tmp_path):
    reg = CameraRegistry(tmp_path)
    slugs = [
        reg.resolve(_identity(f"hw:{i}", "HD USB Camera")) for i in range(3)
    ]
    assert len(set(slugs)) == 3
    assert slugs[0] == "hd-usb-camera"


# ---------------------------------------------------------------------------
# On-disk format
# ---------------------------------------------------------------------------


def test_on_disk_format_is_versioned(tmp_path):
    reg = CameraRegistry(tmp_path)
    slug = reg.resolve(_identity("hw:a", "Cam A"))
    data = json.loads((tmp_path / REGISTRY_FILENAME).read_text())
    assert data["version"] == REGISTRY_VERSION
    assert "hw:a" in data["cameras"]
    assert data["cameras"]["hw:a"]["slug"] == slug


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_satisfies_camera_registry_protocol(tmp_path):
    reg = CameraRegistry(tmp_path)
    assert isinstance(reg, CameraRegistryProtocol)


# ---------------------------------------------------------------------------
# Self-healing location-duplicate reconciliation (ticket 021-007)
#
# A production registry on this machine was found with every physical
# camera registered twice -- once under `avf:<id>`, once under
# `vidpid:<vid>:<pid>@<id>` for the *same* id -- because
# `camera/identity.py`'s pre-fix resolver could route the same camera
# through either tier depending on whether a system_profiler name-match
# happened to land. Fixing the resolver (see test_identity.py) does not
# retroactively clean a registry.json that already has both records; these
# tests cover the registry-side self-heal that does.
# ---------------------------------------------------------------------------


def _write_raw_registry(tmp_path, cameras: dict, next_number: int) -> None:
    data = {
        "version": REGISTRY_VERSION,
        "next_number": next_number,
        "cameras": cameras,
    }
    (tmp_path / REGISTRY_FILENAME).write_text(json.dumps(data))


def test_load_merges_avf_and_vidpid_duplicates_sharing_a_location(tmp_path):
    """The exact shape found in the wild: an `avf:` record (lower
    number, earlier-assigned) and a `vidpid:...@<same location>` record
    (higher number, minted later by the bug) collapse into one on
    load -- the lower-numbered survivor keeps its slug and number."""
    _write_raw_registry(
        tmp_path,
        {
            "avf:0x1100000046d0946": {
                "hardware_key": "avf:0x1100000046d0946",
                "slug": "brio-501",
                "number": 1,
                "device_name": "Brio 501",
                "last_seen": "2026-08-01T18:13:27+00:00",
            },
            "vidpid:046d:0946@0x1100000046d0946": {
                "hardware_key": "vidpid:046d:0946@0x1100000046d0946",
                "slug": "brio-501-2",
                "number": 7,
                "device_name": "Brio 501",
                "last_seen": "2026-08-01T17:21:02+00:00",
            },
        },
        next_number=9,
    )

    reg = CameraRegistry(tmp_path)

    assert len(reg) == 1
    survivor = reg.get("brio-501")
    assert survivor is not None
    assert survivor.number == 1
    assert survivor.hardware_key == "avf:0x1100000046d0946"
    assert reg.get("brio-501-2") is None
    assert reg.get_by_number(7) is None  # a permanent gap, not reissued

    # The cleanup is persisted, not just an in-memory view.
    reg2 = CameraRegistry(tmp_path)
    assert len(reg2) == 1
    assert reg2.get("brio-501").number == 1


def test_reconcile_keeps_the_freshest_device_name_and_last_seen(tmp_path):
    """The survivor's device_name/last_seen reflect whichever duplicate
    was actually seen most recently -- not always the lower-numbered
    one's own stale data."""
    _write_raw_registry(
        tmp_path,
        {
            "avf:0xdeadbeef": {
                "hardware_key": "avf:0xdeadbeef",
                "slug": "cam-a",
                "number": 1,
                "device_name": "Cam A (old name)",
                "last_seen": "2026-06-01T00:00:00+00:00",
            },
            "vidpid:0c45:6366@0xdeadbeef": {
                "hardware_key": "vidpid:0c45:6366@0xdeadbeef",
                "slug": "cam-a-2",
                "number": 4,
                "device_name": "Cam A (current name)",
                "last_seen": "2026-08-01T12:00:00+00:00",
            },
        },
        next_number=5,
    )

    reg = CameraRegistry(tmp_path)

    survivor = reg.get("cam-a")
    assert survivor.device_name == "Cam A (current name)"
    assert survivor.last_seen == "2026-08-01T12:00:00+00:00"


def test_reconcile_does_not_merge_distinct_locations(tmp_path):
    """Two cameras of the same model on two different ports keep two
    records -- their location fragments genuinely differ, so this is
    not the duplicate-registration bug's shape."""
    _write_raw_registry(
        tmp_path,
        {
            "vidpid:0c45:6366@loc1": {
                "hardware_key": "vidpid:0c45:6366@loc1",
                "slug": "arducam-ov9782-usb-camera",
                "number": 1,
                "device_name": "Arducam OV9782 USB Camera",
                "last_seen": "2026-08-01T00:00:00+00:00",
            },
            "vidpid:0c45:6366@loc2": {
                "hardware_key": "vidpid:0c45:6366@loc2",
                "slug": "arducam-ov9782-usb-camera-2",
                "number": 2,
                "device_name": "Arducam OV9782 USB Camera",
                "last_seen": "2026-08-01T00:00:00+00:00",
            },
        },
        next_number=3,
    )

    reg = CameraRegistry(tmp_path)

    assert len(reg) == 2
    assert reg.get("arducam-ov9782-usb-camera") is not None
    assert reg.get("arducam-ov9782-usb-camera-2") is not None


def test_reconcile_ignores_non_location_tiers(tmp_path):
    """`serial:`/`name:` keys are never location-derived and must never
    be merged with each other or with a location-based key, even if
    (implausibly) their fragments matched textually."""
    _write_raw_registry(
        tmp_path,
        {
            "serial:SN1": {
                "hardware_key": "serial:SN1",
                "slug": "cam-a",
                "number": 1,
                "device_name": "Cam A",
                "last_seen": "2026-08-01T00:00:00+00:00",
            },
            "name:cam-a": {
                "hardware_key": "name:cam-a",
                "slug": "cam-a-2",
                "number": 2,
                "device_name": "Cam A",
                "last_seen": "2026-08-01T00:00:00+00:00",
            },
        },
        next_number=3,
    )

    reg = CameraRegistry(tmp_path)

    assert len(reg) == 2


def test_reconcile_is_idempotent(tmp_path):
    """A second load of an already-reconciled registry changes nothing
    further -- no re-merging, no renumbering."""
    _write_raw_registry(
        tmp_path,
        {
            "avf:0xabc": {
                "hardware_key": "avf:0xabc",
                "slug": "cam-a",
                "number": 1,
                "device_name": "Cam A",
                "last_seen": "2026-08-01T00:00:00+00:00",
            },
            "vidpid:0c45:6366@0xabc": {
                "hardware_key": "vidpid:0c45:6366@0xabc",
                "slug": "cam-a-2",
                "number": 2,
                "device_name": "Cam A",
                "last_seen": "2026-08-01T00:00:00+00:00",
            },
        },
        next_number=3,
    )

    reg = CameraRegistry(tmp_path)
    assert len(reg) == 1
    first_pass_data = json.loads((tmp_path / REGISTRY_FILENAME).read_text())

    reg2 = CameraRegistry(tmp_path)
    assert len(reg2) == 1
    second_pass_data = json.loads((tmp_path / REGISTRY_FILENAME).read_text())

    assert first_pass_data == second_pass_data


def test_reconcile_three_way_duplicate_keeps_lowest_number(tmp_path):
    """avf:/vidpid:/loc: all sharing one location -- the full three-tier
    duplication shape -- collapses to a single survivor."""
    _write_raw_registry(
        tmp_path,
        {
            "loc:0xabc": {
                "hardware_key": "loc:0xabc",
                "slug": "cam-a-3",
                "number": 3,
                "device_name": "Cam A",
                "last_seen": "2026-08-01T00:00:00+00:00",
            },
            "avf:0xabc": {
                "hardware_key": "avf:0xabc",
                "slug": "cam-a",
                "number": 1,
                "device_name": "Cam A",
                "last_seen": "2026-08-01T00:00:00+00:00",
            },
            "vidpid:0c45:6366@0xabc": {
                "hardware_key": "vidpid:0c45:6366@0xabc",
                "slug": "cam-a-2",
                "number": 2,
                "device_name": "Cam A",
                "last_seen": "2026-08-01T00:00:00+00:00",
            },
        },
        next_number=4,
    )

    reg = CameraRegistry(tmp_path)

    assert len(reg) == 1
    assert reg.get("cam-a").number == 1
