"""Tests for ``aprilcam.config.loader`` (ticket 022-001).

Headless and file-only: every test drives :class:`ConfigLoader` against
real JSON fixtures on disk (the shipped ``main-playfield.json`` plus
``tmp_path``-written scratch documents) -- no daemon, no camera, no
network. Covers the acceptance criteria that are testable purely at the
``ConfigLoader`` layer: the real fixture's full feature set, whole-
document rejection (duplicate id, no name, malformed JSON, out-of-bounds
feature), and the ``load(x.to_dict()) == x`` round trip for both
document types.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from aprilcam.config.directories import Directories
from aprilcam.config.interfaces import CameraConfig
from aprilcam.config.loader import ConfigLoader, PlayfieldFeatureEntry
from aprilcam.errors import MalformedDocument
from aprilcam.types import WorldPoint

REPO_ROOT = Path(__file__).resolve().parents[3]
MAIN_PLAYFIELD_PATH = REPO_ROOT / "config" / "aprilcam-old" / "playfields" / "main-playfield.json"


@pytest.fixture
def loader() -> ConfigLoader:
    return ConfigLoader()


def _write(tmp_path: Path, name: str, data: dict) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _minimal_playfield_doc(**overrides) -> dict:
    doc = {
        "name": "test-field",
        "playfield": {"width_cm": 100.0, "height_cm": 80.0},
        "april_tags": [{"slug": "a1", "id": 1, "x": 0, "y": 0}],
        "aruco_tags": [
            {"slug": "u1", "id": 1, "x": -40, "y": 30},
            {"slug": "u2", "id": 2, "x": 40, "y": -30},
        ],
        "rectangles": [{"slug": "r1", "x": 10, "y": 10, "color": "blue"}],
        "dots": [{"slug": "d1", "x": -10, "y": -10, "color": "red"}],
    }
    doc.update(overrides)
    return doc


# ---------------------------------------------------------------------------
# The real fixture -- AC1 (UC-02.S-1): full feature set, identical coords.
# ---------------------------------------------------------------------------


def test_main_playfield_loads_with_all_25_features_and_identical_coordinates(loader):
    assert MAIN_PLAYFIELD_PATH.exists(), f"fixture missing: {MAIN_PLAYFIELD_PATH}"

    definition = loader.load_playfield(MAIN_PLAYFIELD_PATH)

    assert definition.name == "main-playfield"
    assert definition.width_cm == 134.3
    assert definition.height_cm == 89.3

    features = definition.features()
    assert len(features) == 25  # 1 AprilTag + 10 ArUco + 6 rectangles + 8 dots

    by_slug = {f.slug: f for f in features}
    aruco_1 = by_slug["aruco-northwest-u1"]
    assert aruco_1.kind == "aruco_tag"
    assert aruco_1.position == WorldPoint(x=-67, y=44.65)

    aruco_5 = by_slug["aruco-southeast-u5"]
    assert aruco_5.position == WorldPoint(x=67, y=-44.65)

    dot_west_blue = by_slug["dot-west-blue"]
    assert dot_west_blue.kind == "dot"
    assert dot_west_blue.colour == "blue"
    assert dot_west_blue.position == WorldPoint(x=-50, y=0)

    april_tag_1 = by_slug["apriltag-center-a1"]
    assert april_tag_1.kind == "april_tag"
    assert april_tag_1.position == WorldPoint(x=0, y=0)


def test_main_playfield_required_aruco_ids_is_the_full_perimeter_plus_interior_set(loader):
    definition = loader.load_playfield(MAIN_PLAYFIELD_PATH)

    assert definition.required_aruco_ids() == (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)


def test_main_playfield_aruco_position_matches_the_surveyed_feature(loader):
    """Ticket 003's calibration-matching lookup: id -> WorldPoint,
    which ``features()``/``Feature`` alone cannot answer (no numeric id
    on ``Feature``)."""
    definition = loader.load_playfield(MAIN_PLAYFIELD_PATH)

    assert definition.aruco_position(1) == WorldPoint(x=-67, y=44.65)
    assert definition.aruco_position(5) == WorldPoint(x=67, y=-44.65)
    assert definition.aruco_position(9) == WorldPoint(x=-35, y=0)
    assert definition.aruco_position(10) == WorldPoint(x=35, y=0)

    with pytest.raises(KeyError):
        definition.aruco_position(99)


# ---------------------------------------------------------------------------
# Round trip -- AC (Testing section): load(x.to_dict()) == x
# ---------------------------------------------------------------------------


def test_playfield_round_trip_load_of_to_dict_equals_original(loader, tmp_path):
    definition = loader.load_playfield(MAIN_PLAYFIELD_PATH)

    dumped_path = tmp_path / "dumped-playfield.json"
    dumped_path.write_text(json.dumps(definition.to_dict()), encoding="utf-8")
    reloaded = loader.load_playfield(dumped_path)

    assert reloaded == definition
    assert reloaded.features() == definition.features()
    assert reloaded.required_aruco_ids() == definition.required_aruco_ids()


def test_camera_config_round_trip_load_of_to_dict_equals_original(loader, tmp_path):
    original = CameraConfig(
        name="arducam-ov9782-usb-camera",
        playfield="main-playfield",
        settings={"exposure": 100, "gain": 2.5},
    )

    dumped_path = tmp_path / "dumped-camera.json"
    dumped_path.write_text(json.dumps(original.to_dict()), encoding="utf-8")
    reloaded = loader.load_camera_config(dumped_path)

    assert reloaded == original


# ---------------------------------------------------------------------------
# Whole-document rejection -- AC2 (UC-02.S-2): duplicate id, previous
# definition untouched (tested here at the loader layer per the
# ticket's own instruction: "the daemon never sees a file the loader
# would reject").
# ---------------------------------------------------------------------------


def test_duplicate_aruco_id_at_different_coordinates_is_rejected_naming_the_id(loader, tmp_path):
    doc = _minimal_playfield_doc(
        aruco_tags=[
            {"slug": "u1", "id": 3, "x": -40, "y": 30},
            {"slug": "u1-dup", "id": 3, "x": 40, "y": -30},
        ]
    )
    path = _write(tmp_path, "dup.json", doc)

    with pytest.raises(MalformedDocument) as excinfo:
        loader.load_playfield(path)

    assert "3" in str(excinfo.value)
    assert "ArUco" in str(excinfo.value)


def test_duplicate_aruco_id_at_the_same_coordinates_is_allowed(loader, tmp_path):
    """Redundant, not conflicting -- only a genuine coordinate mismatch
    is rejected (see the module docstring)."""
    doc = _minimal_playfield_doc(
        aruco_tags=[
            {"slug": "u1", "id": 3, "x": -40, "y": 30},
            {"slug": "u1-again", "id": 3, "x": -40, "y": 30},
        ]
    )
    path = _write(tmp_path, "same.json", doc)

    definition = loader.load_playfield(path)

    assert len(definition.features()) == 5  # 1 april + 2 aruco + 1 rect + 1 dot


def test_duplicate_april_tag_id_at_different_coordinates_is_rejected(loader, tmp_path):
    doc = _minimal_playfield_doc(
        april_tags=[
            {"slug": "a1", "id": 1, "x": 0, "y": 0},
            {"slug": "a1-dup", "id": 1, "x": 5, "y": 5},
        ]
    )
    path = _write(tmp_path, "dup-april.json", doc)

    with pytest.raises(MalformedDocument) as excinfo:
        loader.load_playfield(path)

    assert "AprilTag" in str(excinfo.value)
    assert "1" in str(excinfo.value)


def test_aruco_and_april_tag_may_share_the_same_numeric_id(loader, tmp_path):
    """AprilTag 1 and ArUco 1 are different identities (types.TagFamily's
    own docstring) -- a shared numeric id across families is not a
    conflict."""
    doc = _minimal_playfield_doc(
        april_tags=[{"slug": "a1", "id": 1, "x": 0, "y": 0}],
        aruco_tags=[{"slug": "u1", "id": 1, "x": -40, "y": 30}],
    )
    path = _write(tmp_path, "shared-id.json", doc)

    definition = loader.load_playfield(path)

    assert definition.required_aruco_ids() == (1,)


def test_rejected_push_never_produces_an_object_to_push(loader, tmp_path):
    """UC-02.S-2's client-side half: a malformed document never becomes
    a ``PlayfieldDefinition`` at all, so there is nothing a caller could
    even hand to ``Daemon.set_playfield`` -- "a bad file never reaches
    the daemon" is structural here, not a policy the daemon has to
    enforce. The daemon-side half (a *good* definition set earlier is
    unaffected by a later rejected push) is covered in
    ``tests/aprilcam/daemon/test_service.py``, against a real
    ``DaemonService``."""
    bad_doc = _minimal_playfield_doc(
        aruco_tags=[
            {"slug": "u1", "id": 3, "x": -40, "y": 30},
            {"slug": "u1-dup", "id": 3, "x": 40, "y": -30},
        ]
    )
    bad_path = _write(tmp_path, "bad.json", bad_doc)

    with pytest.raises(MalformedDocument):
        loader.load_playfield(bad_path)


# ---------------------------------------------------------------------------
# Whole-document rejection -- AC3 (UC-23.S-4): no name, rejected
# client-side, before any RPC.
# ---------------------------------------------------------------------------


def test_playfield_with_no_name_is_rejected_by_the_loader(loader, tmp_path):
    doc = _minimal_playfield_doc()
    del doc["name"]
    path = _write(tmp_path, "no-name.json", doc)

    with pytest.raises(MalformedDocument) as excinfo:
        loader.load_playfield(path)
    assert "name" in str(excinfo.value)


def test_camera_config_with_no_name_is_rejected_by_the_loader(loader, tmp_path):
    doc = {"playfield": "main-playfield", "settings": {}}
    path = _write(tmp_path, "no-name-camera.json", doc)

    with pytest.raises(MalformedDocument) as excinfo:
        loader.load_camera_config(path)
    assert "name" in str(excinfo.value)


def test_playfield_with_blank_name_is_rejected(loader, tmp_path):
    doc = _minimal_playfield_doc(name="   ")
    path = _write(tmp_path, "blank-name.json", doc)

    with pytest.raises(MalformedDocument):
        loader.load_playfield(path)


# ---------------------------------------------------------------------------
# Other whole-document malformations
# ---------------------------------------------------------------------------


def test_invalid_json_is_rejected(loader, tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(MalformedDocument):
        loader.load_playfield(path)

    with pytest.raises(MalformedDocument):
        loader.load_camera_config(path)


def test_missing_file_is_rejected(loader, tmp_path):
    with pytest.raises(MalformedDocument):
        loader.load_playfield(tmp_path / "does-not-exist.json")


def test_playfield_missing_geometry_block_is_rejected(loader, tmp_path):
    doc = _minimal_playfield_doc()
    del doc["playfield"]
    path = _write(tmp_path, "no-geometry.json", doc)

    with pytest.raises(MalformedDocument):
        loader.load_playfield(path)


def test_playfield_non_positive_field_size_is_rejected(loader, tmp_path):
    doc = _minimal_playfield_doc(playfield={"width_cm": 0, "height_cm": 80.0})
    path = _write(tmp_path, "zero-width.json", doc)

    with pytest.raises(MalformedDocument):
        loader.load_playfield(path)


def test_feature_outside_field_bounds_is_rejected_field_size_corner_inconsistency(loader, tmp_path):
    doc = _minimal_playfield_doc(
        aruco_tags=[{"slug": "u1", "id": 1, "x": -999, "y": 0}],
    )
    path = _write(tmp_path, "out-of-bounds.json", doc)

    with pytest.raises(MalformedDocument) as excinfo:
        loader.load_playfield(path)
    assert "u1" in str(excinfo.value)


def test_feature_exactly_on_the_boundary_is_accepted(loader, tmp_path):
    """The real fixture's corner ArUcos sit exactly on the field's
    half-extent (e.g. y = height_cm/2) -- a strict less-than bounds
    check would wrongly reject them, so this pins the inclusive
    comparison."""
    doc = _minimal_playfield_doc(
        playfield={"width_cm": 100.0, "height_cm": 80.0},
        aruco_tags=[{"slug": "u1", "id": 1, "x": -50, "y": 40}],
    )
    path = _write(tmp_path, "on-boundary.json", doc)

    definition = loader.load_playfield(path)
    assert definition.aruco_position(1) == WorldPoint(x=-50, y=40)


def test_playfield_entry_missing_slug_is_rejected(loader, tmp_path):
    doc = _minimal_playfield_doc(aruco_tags=[{"id": 1, "x": 0, "y": 0}])
    path = _write(tmp_path, "no-slug.json", doc)

    with pytest.raises(MalformedDocument):
        loader.load_playfield(path)


def test_playfield_tag_entry_missing_id_is_rejected(loader, tmp_path):
    doc = _minimal_playfield_doc(aruco_tags=[{"slug": "u1", "x": 0, "y": 0}])
    path = _write(tmp_path, "no-id.json", doc)

    with pytest.raises(MalformedDocument):
        loader.load_playfield(path)


def test_playfield_entry_with_non_list_section_is_rejected(loader, tmp_path):
    doc = _minimal_playfield_doc(aruco_tags="not-a-list")
    path = _write(tmp_path, "bad-section.json", doc)

    with pytest.raises(MalformedDocument):
        loader.load_playfield(path)


# ---------------------------------------------------------------------------
# CameraConfig loading (the link slice)
# ---------------------------------------------------------------------------


def test_camera_config_loads_name_playfield_and_settings(loader, tmp_path):
    doc = {
        "name": "arducam-ov9782-usb-camera",
        "playfield": "main-playfield",
        "settings": {"exposure": 100},
    }
    path = _write(tmp_path, "camera.json", doc)

    config = loader.load_camera_config(path)

    assert config.name == "arducam-ov9782-usb-camera"
    assert config.playfield == "main-playfield"
    assert config.settings == {"exposure": 100}


def test_camera_config_playfield_defaults_to_none_when_absent(loader, tmp_path):
    doc = {"name": "hd-usb-camera"}
    path = _write(tmp_path, "camera-no-playfield.json", doc)

    config = loader.load_camera_config(path)

    assert config.playfield is None
    assert config.settings == {}


def test_camera_config_playfield_explicit_null_is_none(loader, tmp_path):
    doc = {"name": "hd-usb-camera", "playfield": None}
    path = _write(tmp_path, "camera-null-playfield.json", doc)

    config = loader.load_camera_config(path)
    assert config.playfield is None


def test_camera_config_non_string_playfield_is_rejected(loader, tmp_path):
    doc = {"name": "hd-usb-camera", "playfield": 42}
    path = _write(tmp_path, "camera-bad-playfield.json", doc)

    with pytest.raises(MalformedDocument):
        loader.load_camera_config(path)


def test_camera_config_non_object_settings_is_rejected(loader, tmp_path):
    doc = {"name": "hd-usb-camera", "settings": "not-an-object"}
    path = _write(tmp_path, "camera-bad-settings.json", doc)

    with pytest.raises(MalformedDocument):
        loader.load_camera_config(path)


# ---------------------------------------------------------------------------
# PlayfieldFeatureEntry -- the small internal type calibration relies on
# ---------------------------------------------------------------------------


def test_playfield_feature_entry_is_frozen_and_kw_only():
    entry = PlayfieldFeatureEntry(slug="s", x=1.0, y=2.0, tag_id=3, colour="red")
    assert entry.slug == "s" and entry.tag_id == 3 and entry.colour == "red"
    with pytest.raises(Exception):
        entry.x = 99.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ConfigLoader.load_config_dir -- directory scan (ticket 023-003)
# ---------------------------------------------------------------------------


def test_load_config_dir_returns_successes_and_names_the_malformed_files(loader, tmp_path):
    """AC1: a directory-scan function reading playfields/*.json then
    cameras/*/config.json, returning successes plus named per-file
    errors -- never raising for one bad file."""
    config_dir = tmp_path / "config"
    (config_dir / "playfields").mkdir(parents=True)
    (config_dir / "cameras" / "arducam-ov9782-usb-camera").mkdir(parents=True)
    (config_dir / "cameras" / "hd-usb-camera").mkdir(parents=True)

    (config_dir / "playfields" / "good-field.json").write_text(
        json.dumps(_minimal_playfield_doc(name="good-field")), encoding="utf-8"
    )
    (config_dir / "playfields" / "broken.json").write_text("{not valid json", encoding="utf-8")
    (config_dir / "cameras" / "arducam-ov9782-usb-camera" / "config.json").write_text(
        json.dumps(
            {"name": "arducam-ov9782-usb-camera", "playfield": "good-field", "settings": {}}
        ),
        encoding="utf-8",
    )
    # hd-usb-camera's config.json is present but malformed (no 'name').
    (config_dir / "cameras" / "hd-usb-camera" / "config.json").write_text(
        json.dumps({"playfield": "good-field"}), encoding="utf-8"
    )

    directories = Directories(config_dir=config_dir, state_dir=tmp_path / "state")

    result = loader.load_config_dir(directories)

    assert [p.name for p in result.playfields] == ["good-field"]
    assert [c.name for c in result.camera_configs] == ["arducam-ov9782-usb-camera"]

    assert len(result.errors) == 2
    broken_playfield_errors = [e for e in result.errors if e.path.name == "broken.json"]
    assert len(broken_playfield_errors) == 1

    broken_camera_errors = [e for e in result.errors if e.path.parent.name == "hd-usb-camera"]
    assert len(broken_camera_errors) == 1
    assert "name" in broken_camera_errors[0].message


def test_load_config_dir_on_an_empty_config_dir_returns_nothing_and_no_errors(loader, tmp_path):
    """A missing playfields/cameras subdirectory is the ordinary
    first-run case (UC-01.S-2), not an error."""
    directories = Directories(config_dir=tmp_path / "nonexistent-config", state_dir=tmp_path / "state")

    result = loader.load_config_dir(directories)

    assert result.playfields == ()
    assert result.camera_configs == ()
    assert result.errors == ()


def test_load_config_dir_ignores_a_camera_directory_with_no_config_json(loader, tmp_path):
    """A ``cameras/<slug>/`` directory that exists but holds no
    ``config.json`` yet is skipped silently -- nothing there has failed
    to load."""
    config_dir = tmp_path / "config"
    (config_dir / "cameras" / "not-configured-yet").mkdir(parents=True)

    directories = Directories(config_dir=config_dir, state_dir=tmp_path / "state")
    result = loader.load_config_dir(directories)

    assert result.camera_configs == ()
    assert result.errors == ()


def test_load_config_dir_loads_the_real_main_playfield_fixture(loader, tmp_path):
    """The full-fixture round trip: the real checked-in main-playfield.json,
    installed at the layout config.md documents, loads through the
    directory scan exactly as :meth:`ConfigLoader.load_playfield` alone
    already proves it does (see the tests above)."""
    config_dir = tmp_path / "config"
    (config_dir / "playfields").mkdir(parents=True)
    shutil.copyfile(MAIN_PLAYFIELD_PATH, config_dir / "playfields" / "main-playfield.json")

    directories = Directories(config_dir=config_dir, state_dir=tmp_path / "state")
    result = loader.load_config_dir(directories)

    assert len(result.playfields) == 1
    assert result.playfields[0].name == "main-playfield"
    assert result.errors == ()
