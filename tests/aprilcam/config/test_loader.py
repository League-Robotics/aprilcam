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
from aprilcam.config.interfaces import CameraConfig, CameraPosition
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
# settings value coercion (2026-08-22 hotfix) -- a numeric-looking JSON
# *string* (e.g. camera 3's real on-disk "exposure": "100") must never
# reach Camera.apply_config un-coerced: it survives float(value) at the
# point a value is pushed, but a later diagnostic comparison
# (UvcUtilBackend.describe_set_failure's `minimum <= value <= maximum`)
# does not survive a raw str, and previously raised a bare TypeError
# instead of a typed, catchable error. Rejecting/normalising here, at
# load time, is what stops that class of bug recurring silently for any
# numeric setting, not just exposure.
# ---------------------------------------------------------------------------


def test_camera_config_numeric_string_setting_is_coerced_to_a_number(loader, tmp_path):
    """The exact real-world shape that broke camera 3: an
    accidentally-quoted numeric setting. Coerced to a real ``int``
    rather than rejected -- self-healing, not just error-avoiding."""
    doc = {"name": "arducam-ov9782-usb-camera", "settings": {"exposure": "100"}}
    path = _write(tmp_path, "camera-string-exposure.json", doc)

    config = loader.load_camera_config(path)

    assert config.settings == {"exposure": 100}
    assert isinstance(config.settings["exposure"], int)


def test_camera_config_numeric_string_setting_with_decimal_coerces_to_float(loader, tmp_path):
    doc = {"name": "hd-usb-camera", "settings": {"gain": "2.5"}}
    path = _write(tmp_path, "camera-string-gain.json", doc)

    config = loader.load_camera_config(path)

    assert config.settings == {"gain": 2.5}
    assert isinstance(config.settings["gain"], float)


def test_camera_config_non_numeric_string_setting_is_rejected(loader, tmp_path):
    doc = {"name": "hd-usb-camera", "settings": {"exposure": "not-a-number"}}
    path = _write(tmp_path, "camera-garbage-exposure.json", doc)

    with pytest.raises(MalformedDocument, match="exposure"):
        loader.load_camera_config(path)


def test_camera_config_boolean_setting_is_rejected(loader, tmp_path):
    """A JSON boolean is never silently coerced to 1.0/0.0 -- nothing in
    this schema has ever meant a bool for a settings value."""
    doc = {"name": "hd-usb-camera", "settings": {"auto_exposure": True}}
    path = _write(tmp_path, "camera-bool-setting.json", doc)

    with pytest.raises(MalformedDocument, match="auto_exposure"):
        loader.load_camera_config(path)


def test_camera_config_none_setting_stays_none(loader, tmp_path):
    """The blank-template placeholder (CameraConfig.settings' own
    docstring) is untouched by coercion."""
    doc = {"name": "hd-usb-camera", "settings": {"exposure": None}}
    path = _write(tmp_path, "camera-blank-setting.json", doc)

    config = loader.load_camera_config(path)

    assert config.settings == {"exposure": None}


def test_camera_config_already_numeric_setting_is_untouched(loader, tmp_path):
    """An already-well-typed file's shape is never perturbed by this
    pass -- a float stays a float, an int stays an int."""
    doc = {"name": "hd-usb-camera", "settings": {"exposure": 100, "gain": 2.5}}
    path = _write(tmp_path, "camera-numeric-settings.json", doc)

    config = loader.load_camera_config(path)

    assert config.settings == {"exposure": 100, "gain": 2.5}
    assert isinstance(config.settings["exposure"], int)
    assert isinstance(config.settings["gain"], float)


def test_parse_camera_config_rejects_non_numeric_setting_too(loader):
    """The dict-based entry point (MCP's ``set_camera_config`` tool)
    runs the identical coercion/validation -- not just the file-based
    loader."""
    document = {"name": "hd-usb-camera", "settings": {"exposure": "abc"}}

    with pytest.raises(MalformedDocument, match="exposure"):
        loader.parse_camera_config(document)


# ---------------------------------------------------------------------------
# camera_position (sprint 029 ticket 001) -- nadir + optical height, the
# two inputs calibration.geometry.project_tag_to_field_plane needs.
# ---------------------------------------------------------------------------


def test_camera_config_loads_camera_position_nadir_and_height(loader, tmp_path):
    doc = {
        "name": "arducam-ov9782-usb-camera",
        "camera_position": {"nadir_x_cm": 3.05, "nadir_y_cm": -2.75, "height_cm": 128.25},
    }
    path = _write(tmp_path, "camera-with-position.json", doc)

    config = loader.load_camera_config(path)

    assert config.camera_position == CameraPosition(
        nadir=WorldPoint(x=3.05, y=-2.75), height_cm=128.25
    )


def test_camera_config_camera_position_absent_parses_as_none(loader, tmp_path):
    """A pre-029 config file (no ``camera_position`` key at all) parses
    unchanged -- explicit regression test, not just "no test failed"
    (the ticket's own acceptance criterion)."""
    doc = {"name": "hd-usb-camera", "playfield": "main-playfield", "settings": {"exposure": 100}}
    path = _write(tmp_path, "pre-029-camera.json", doc)

    config = loader.load_camera_config(path)

    assert config.camera_position is None


def test_camera_config_round_trip_with_camera_position(loader, tmp_path):
    """``load(x.to_dict()) == x`` with ``camera_position`` set -- the
    round-trip contract extended to the new field."""
    original = CameraConfig(
        name="arducam-ov9782-usb-camera",
        playfield="main-playfield",
        settings={"exposure": 100},
        camera_position=CameraPosition(nadir=WorldPoint(x=3.05, y=-2.75), height_cm=128.25),
    )

    dumped_path = tmp_path / "dumped-camera-with-position.json"
    dumped_path.write_text(json.dumps(original.to_dict()), encoding="utf-8")
    reloaded = loader.load_camera_config(dumped_path)

    assert reloaded == original


def test_camera_config_to_dict_omits_camera_position_key_when_unset():
    """``to_dict()`` never emits a ``null`` placeholder -- a config
    document that never configured position round-trips byte-for-byte
    identical to before this ticket."""
    config = CameraConfig(name="hd-usb-camera", playfield=None, settings={})

    assert "camera_position" not in config.to_dict()


def test_camera_config_to_dict_includes_camera_position_when_set():
    config = CameraConfig(
        name="arducam-ov9782-usb-camera",
        playfield=None,
        settings={},
        camera_position=CameraPosition(nadir=WorldPoint(x=3.05, y=-2.75), height_cm=128.25),
    )

    assert config.to_dict()["camera_position"] == {
        "nadir_x_cm": 3.05,
        "nadir_y_cm": -2.75,
        "height_cm": 128.25,
    }


def test_camera_config_camera_position_zero_height_is_rejected_naming_the_problem(loader, tmp_path):
    doc = {
        "name": "arducam-ov9782-usb-camera",
        "camera_position": {"nadir_x_cm": 0.0, "nadir_y_cm": 0.0, "height_cm": 0.0},
    }
    path = _write(tmp_path, "zero-height.json", doc)

    with pytest.raises(MalformedDocument) as excinfo:
        loader.load_camera_config(path)
    assert "height_cm" in str(excinfo.value)


def test_camera_config_camera_position_negative_height_is_rejected(loader, tmp_path):
    doc = {
        "name": "arducam-ov9782-usb-camera",
        "camera_position": {"nadir_x_cm": 0.0, "nadir_y_cm": 0.0, "height_cm": -5.0},
    }
    path = _write(tmp_path, "negative-height.json", doc)

    with pytest.raises(MalformedDocument):
        loader.load_camera_config(path)


def test_camera_config_camera_position_non_object_is_rejected(loader, tmp_path):
    doc = {"name": "arducam-ov9782-usb-camera", "camera_position": "not-an-object"}
    path = _write(tmp_path, "bad-camera-position.json", doc)

    with pytest.raises(MalformedDocument):
        loader.load_camera_config(path)


def test_camera_config_camera_position_missing_field_is_rejected(loader, tmp_path):
    doc = {
        "name": "arducam-ov9782-usb-camera",
        "camera_position": {"nadir_x_cm": 1.0, "height_cm": 100.0},  # nadir_y_cm missing
    }
    path = _write(tmp_path, "incomplete-camera-position.json", doc)

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
# Feature.height_cm / PlayfieldFeatureEntry.height_cm (sprint 030 ticket
# 002) -- schema acceptance criteria: declared field, defaults to 0.0,
# absent JSON key round-trips identically to every playfield on disk
# today, negative values rejected.
# ---------------------------------------------------------------------------


def test_playfield_feature_entry_height_cm_defaults_to_zero():
    entry = PlayfieldFeatureEntry(slug="s", x=1.0, y=2.0, tag_id=3)
    assert entry.height_cm == 0.0


def test_feature_height_cm_defaults_to_zero():
    from aprilcam.config.interfaces import Feature
    from aprilcam.types import WorldPoint as WP

    feature = Feature(slug="s", kind="aruco_tag", position=WP(x=0.0, y=0.0))
    assert feature.height_cm == 0.0


def test_main_playfield_every_feature_has_height_cm_zero(loader):
    """AC3 (schema): every playfield file on disk today (no ``height_cm``
    keys at all) loads and behaves identically -- every feature's
    ``height_cm`` is the default ``0.0``."""
    definition = loader.load_playfield(MAIN_PLAYFIELD_PATH)
    for feature in definition.features():
        assert feature.height_cm == 0.0
    for entry in definition.aruco_tags + definition.april_tags:
        assert entry.height_cm == 0.0


def test_aruco_tag_entry_with_height_cm_key_parses_the_value(loader, tmp_path):
    doc = _minimal_playfield_doc(
        aruco_tags=[
            {"slug": "u1", "id": 1, "x": -40, "y": 30, "height_cm": 12.5},
            {"slug": "u2", "id": 2, "x": 40, "y": -30},
        ]
    )
    path = _write(tmp_path, "elevated.json", doc)

    definition = loader.load_playfield(path)

    by_slug = {e.slug: e for e in definition.aruco_tags}
    assert by_slug["u1"].height_cm == 12.5
    assert by_slug["u2"].height_cm == 0.0  # absent key -> default

    feature_by_slug = {f.slug: f for f in definition.features()}
    assert feature_by_slug["u1"].height_cm == 12.5


def test_april_tag_entry_with_height_cm_key_parses_the_value(loader, tmp_path):
    doc = _minimal_playfield_doc(
        april_tags=[{"slug": "a1", "id": 1, "x": 0, "y": 0, "height_cm": 20.0}]
    )
    path = _write(tmp_path, "elevated-april.json", doc)

    definition = loader.load_playfield(path)

    assert definition.april_tags[0].height_cm == 20.0


def test_playfield_round_trip_holds_for_an_entry_with_height_cm(loader, tmp_path):
    """AC3: ``load(x.to_dict()) == x`` holds whether or not ``height_cm``
    is present in the source JSON."""
    doc = _minimal_playfield_doc(
        aruco_tags=[
            {"slug": "u1", "id": 1, "x": -40, "y": 30, "height_cm": 15.0},
            {"slug": "u2", "id": 2, "x": 40, "y": -30},
        ]
    )
    path = _write(tmp_path, "elevated-roundtrip.json", doc)
    definition = loader.load_playfield(path)

    dumped_path = tmp_path / "dumped-elevated.json"
    dumped_path.write_text(json.dumps(definition.to_dict()), encoding="utf-8")
    reloaded = loader.load_playfield(dumped_path)

    assert reloaded == definition
    assert reloaded.features() == definition.features()


def test_playfield_round_trip_holds_for_an_entry_without_height_cm(loader, tmp_path):
    """The counterpart case: no entry declares ``height_cm`` at all
    (every playfield on disk today) -- the round trip holds identically
    to before this ticket."""
    path = _write(tmp_path, "coplanar-roundtrip.json", _minimal_playfield_doc())
    definition = loader.load_playfield(path)

    dumped_path = tmp_path / "dumped-coplanar.json"
    dumped_path.write_text(json.dumps(definition.to_dict()), encoding="utf-8")
    reloaded = loader.load_playfield(dumped_path)

    assert reloaded == definition
    assert "height_cm" not in json.loads(dumped_path.read_text())["aruco_tags"][0]


def test_playfield_feature_negative_height_cm_is_rejected(loader, tmp_path):
    doc = _minimal_playfield_doc(
        aruco_tags=[{"slug": "u1", "id": 1, "x": -40, "y": 30, "height_cm": -5.0}]
    )
    path = _write(tmp_path, "negative-height.json", doc)

    with pytest.raises(MalformedDocument, match="height_cm"):
        loader.load_playfield(path)


def test_playfield_feature_invalid_height_cm_type_is_rejected(loader, tmp_path):
    doc = _minimal_playfield_doc(
        aruco_tags=[{"slug": "u1", "id": 1, "x": -40, "y": 30, "height_cm": "not-a-number"}]
    )
    path = _write(tmp_path, "bad-height-type.json", doc)

    with pytest.raises(MalformedDocument, match="height_cm"):
        loader.load_playfield(path)


def test_to_dict_omits_height_cm_when_zero_includes_when_nonzero():
    zero = PlayfieldFeatureEntry(slug="u1", x=1.0, y=2.0, tag_id=1)
    elevated = PlayfieldFeatureEntry(slug="u2", x=3.0, y=4.0, tag_id=2, height_cm=8.0)
    from aprilcam.config.loader import PlayfieldDefinition as ConcretePlayfieldDefinition

    definition = ConcretePlayfieldDefinition(
        name="t", width_cm=100.0, height_cm=100.0, aruco_tags=(zero, elevated)
    )

    data = definition.to_dict()

    assert "height_cm" not in data["aruco_tags"][0]
    assert data["aruco_tags"][1]["height_cm"] == 8.0


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


# ---------------------------------------------------------------------------
# Sprint 045 ticket 001 -- marker size and required/helpful policy.
# ---------------------------------------------------------------------------

SECONDARY_PLAYFIELD_PATH = MAIN_PLAYFIELD_PATH.parent / "secondary-playfield.json"
SHIPPING_DIR = REPO_ROOT / "config" / "aprilcam" / "playfields"


def _doc_with(entry_extra: dict | None = None, field_extra: dict | None = None) -> dict:
    doc = _minimal_playfield_doc()
    doc["aruco_tags"][0].update(entry_extra or {})
    doc["playfield"].update(field_extra or {})
    return doc


def test_marker_size_defaults_absent_and_resolves_none(loader):
    d = loader.parse_playfield(_minimal_playfield_doc())
    assert d.marker_size_mm_default is None
    assert d.marker_size_mm(1) is None
    assert d.aruco_tags[0].size_mm is None and d.aruco_tags[0].policy is None
    assert d.required_marker_ids() == (1, 2)
    assert d.helpful_marker_ids() == ()
    assert d.required_aruco_ids() == (1, 2)


def test_entry_size_overrides_field_default(loader):
    d = loader.parse_playfield(_doc_with({"size_mm": 30}, {"marker_size_mm": 46}))
    assert d.marker_size_mm(1) == 30.0
    assert d.marker_size_mm(2) == 46.0
    with pytest.raises(KeyError):
        d.marker_size_mm(99)


def test_policy_partitions_ids_and_required_aruco_ids_unchanged(loader):
    d = loader.parse_playfield(_doc_with({"policy": "helpful"}))
    assert d.required_marker_ids() == (2,)
    assert d.helpful_marker_ids() == (1,)
    assert d.required_aruco_ids() == (1, 2)


def test_new_keys_round_trip_and_are_omitted_when_unset(loader):
    d = loader.parse_playfield(_doc_with({"size_mm": 30, "policy": "helpful"}, {"marker_size_mm": 46}))
    out = d.to_dict()
    assert out["playfield"]["marker_size_mm"] == 46.0
    assert out["aruco_tags"][0]["size_mm"] == 30.0
    assert out["aruco_tags"][0]["policy"] == "helpful"
    assert "size_mm" not in out["aruco_tags"][1] and "policy" not in out["aruco_tags"][1]
    assert loader.parse_playfield(out) == d
    plain = loader.parse_playfield(_minimal_playfield_doc()).to_dict()
    assert "marker_size_mm" not in plain["playfield"]
    assert plain == loader.parse_playfield(plain).to_dict()


@pytest.mark.parametrize("bad", [0, -1, True, float("nan"), float("inf"), 1000.5, "46", None])
def test_invalid_entry_size_rejected_naming_entry_and_key(loader, bad):
    with pytest.raises(MalformedDocument, match=r"'u1'.*'size_mm'"):
        loader.parse_playfield(_doc_with({"size_mm": bad}))


@pytest.mark.parametrize("bad", [0, -5, False, float("nan"), float("-inf"), 5000, "x"])
def test_invalid_default_size_rejected_naming_key(loader, bad):
    with pytest.raises(MalformedDocument, match="playfield.marker_size_mm"):
        loader.parse_playfield(_doc_with(None, {"marker_size_mm": bad}))


@pytest.mark.parametrize("bad", ["Required", "optional", "", 1, None])
def test_invalid_policy_rejected_naming_entry_and_key(loader, bad):
    with pytest.raises(MalformedDocument, match=r"'u1'.*'policy'"):
        loader.parse_playfield(_doc_with({"policy": bad}))


def test_rotation_deg_is_not_schema_and_is_ignored_like_any_unknown_key(loader):
    d = loader.parse_playfield(_doc_with({"rotation_deg": 90}))
    assert not hasattr(d.aruco_tags[0], "rotation_deg")
    assert "rotation_deg" not in d.to_dict()["aruco_tags"][0]
    assert d == loader.parse_playfield(_minimal_playfield_doc())


def test_rectangles_and_dots_do_not_gain_the_keys(loader):
    doc = _minimal_playfield_doc()
    doc["rectangles"][0].update({"size_mm": 30, "policy": "helpful"})
    d = loader.parse_playfield(doc)
    assert d.rectangles[0].size_mm is None and d.rectangles[0].policy is None
    assert "size_mm" not in d.to_dict()["rectangles"][0]


def test_shipping_main_playfield_resolves_46_for_every_aruco_id(loader):
    d = loader.load_playfield(SHIPPING_DIR / "main-playfield.json")
    assert d.marker_size_mm_default == 46.0
    assert all(d.marker_size_mm(i) == 46.0 for i in d.required_aruco_ids())
    assert d.required_marker_ids() == d.required_aruco_ids()


def test_shipping_secondary_playfield_resolves_none(loader):
    d = loader.load_playfield(SHIPPING_DIR / "secondary-playfield.json")
    assert d.marker_size_mm_default is None
    assert all(d.marker_size_mm(i) is None for i in d.required_aruco_ids())


@pytest.mark.parametrize("name", ["main-playfield.json", "secondary-playfield.json"])
def test_shipping_playfields_round_trip(loader, name):
    d = loader.load_playfield(SHIPPING_DIR / name)
    assert loader.parse_playfield(d.to_dict()) == d


# ---------------------------------------------------------------------------
# calibration_target (sprint 045 ticket 010).
# ---------------------------------------------------------------------------

_BOARD = "6x8:28:22:DICT_4X4_50"


def _target_doc(**target) -> dict:
    doc = _minimal_playfield_doc()
    doc["calibration_target"] = {"kind": "charuco", "board": _BOARD, **target}
    return doc


def test_calibration_target_absent_is_unchanged(loader):
    d = loader.parse_playfield(_minimal_playfield_doc())
    assert d.calibration_target is None
    assert "calibration_target" not in d.to_dict()


def test_calibration_target_defaults_and_round_trip(loader):
    d = loader.parse_playfield(_target_doc())
    t = d.calibration_target
    assert (t.kind, t.board, t.centre_x_cm, t.centre_y_cm, t.rotation_deg) == (
        "charuco", _BOARD, 0.0, 0.0, 0.0)
    d2 = loader.parse_playfield(_target_doc(centre_x_cm=10, centre_y_cm=-5, rotation_deg=90))
    assert d2.calibration_target.rotation_deg == 90.0
    assert loader.parse_playfield(d2.to_dict()) == d2
    assert loader.parse_playfield(d.to_dict()) == d


def test_target_does_not_change_required_marker_ids(loader):
    base = loader.parse_playfield(_minimal_playfield_doc())
    with_t = loader.parse_playfield(_target_doc())
    assert with_t.required_marker_ids() == base.required_marker_ids()
    assert with_t.required_aruco_ids() == base.required_aruco_ids()


def test_target_only_definition_is_valid_with_no_required_markers(loader):
    doc = {"name": "t", "playfield": {"width_cm": 100, "height_cm": 60},
           "calibration_target": {"kind": "charuco", "board": _BOARD}}
    d = loader.parse_playfield(doc)
    assert d.required_marker_ids() == () and d.calibration_target is not None


@pytest.mark.parametrize("target", [
    {"kind": "chessboard"}, {"kind": None},
    {"board": "nonsense"}, {"board": "6x8:28:22"}, {"board": "1x8:28:22:DICT_4X4_50"},
    {"board": "6x8:22:28:DICT_4X4_50"}, {"board": "6x8:-1:-2:DICT_4X4_50"},
    {"board": "6x8:28:22:DICT_NOPE_50"}, {"board": "6x8:28:22:4X4_50"}, {"board": 5},
    {"centre_x_cm": 500}, {"centre_y_cm": -500}, {"centre_x_cm": "a"},
    {"centre_x_cm": True}, {"centre_x_cm": float("nan")},
    {"rotation_deg": float("inf")}, {"rotation_deg": "90"}, {"rotation_deg": None},
])
def test_invalid_calibration_target_rejected(loader, target):
    with pytest.raises(MalformedDocument, match="calibration_target"):
        loader.parse_playfield(_target_doc(**target))


def test_calibration_target_must_be_object(loader):
    doc = _minimal_playfield_doc()
    doc["calibration_target"] = "6x8"
    with pytest.raises(MalformedDocument, match="calibration_target"):
        loader.parse_playfield(doc)


def test_target_centre_allowed_up_to_board_half_size_beyond_field(loader):
    # 6x8 @ 28 mm: half-height 11.2 cm. Field half-height is bounded by the minimal doc.
    h = loader.parse_playfield(_minimal_playfield_doc()).height_cm
    d = loader.parse_playfield(_target_doc(centre_y_cm=h / 2 + 11.0))
    assert d.calibration_target.centre_y_cm == h / 2 + 11.0
