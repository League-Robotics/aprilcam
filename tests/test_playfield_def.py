"""Tests for PlayfieldDefinition and PlayfieldDefinitionRegistry (Sprint 012, Ticket 002)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from aprilcam.core.playfield_def import PlayfieldDefinition, PlayfieldDefinitionRegistry


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

# A minimal playfield JSON matching the main-playfield structure and values.
# Corner ArUco IDs should be [1, 3, 5, 7] (NW/NE/SE/SW) and their world
# coords [(-67, 44.65), (67, 44.65), (67, -44.65), (-67, -44.65)].
_MAIN_PLAYFIELD_DATA = {
    "playfield": {
        "width_cm": 134.3,
        "height_cm": 89.3,
        "origin": "apriltag-center-a1",
        "description": "Origin at AprilTag A1.",
    },
    "april_tags": [
        {"slug": "apriltag-center-a1", "type": "april_tag", "id": 1,
         "cardinal": "center", "x": 0, "y": 0},
    ],
    "aruco_tags": [
        # Corners listed NW, NE, SE, SW so corner_aruco_ids() yields [1, 3, 5, 7]
        # matching the acceptance-criteria order specified in ticket 002.
        {"slug": "aruco-northwest-u1", "type": "aruco_tag", "id": 1,
         "cardinal": "northwest", "x": -67, "y": 44.65},
        {"slug": "aruco-northeast-u3", "type": "aruco_tag", "id": 3,
         "cardinal": "northeast", "x": 67, "y": 44.65},
        {"slug": "aruco-southeast-u5", "type": "aruco_tag", "id": 5,
         "cardinal": "southeast", "x": 67, "y": -44.65},
        {"slug": "aruco-southwest-u7", "type": "aruco_tag", "id": 7,
         "cardinal": "southwest", "x": -67, "y": -44.65},
        # Non-corner markers (not returned by corner_aruco_ids)
        {"slug": "aruco-north-u2", "type": "aruco_tag", "id": 2,
         "cardinal": "north", "x": 0, "y": 44.65},
        {"slug": "aruco-east-u4", "type": "aruco_tag", "id": 4,
         "cardinal": "east", "x": 67, "y": 0},
        {"slug": "aruco-south-u6", "type": "aruco_tag", "id": 6,
         "cardinal": "south", "x": 0, "y": -44.65},
        {"slug": "aruco-west-u8", "type": "aruco_tag", "id": 8,
         "cardinal": "west", "x": -67, "y": 0},
    ],
    "rectangles": [
        {"slug": "rect-east-red", "type": "rectangle", "color": "red",
         "cardinal": "east", "x": 35, "y": 0, "width_cm": 5, "height_cm": 4},
    ],
    "dots": [
        {"slug": "dot-northwest-orange", "type": "dot", "color": "orange",
         "cardinal": "northwest", "size": "large", "x": -50, "y": 30},
    ],
}


def _write_playfield(tmp_path: Path, name: str = "main-playfield", data: dict | None = None) -> Path:
    """Write a playfield JSON fixture to *tmp_path/<name>.json*."""
    if data is None:
        data = _MAIN_PLAYFIELD_DATA
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# PlayfieldDefinition.load
# ---------------------------------------------------------------------------


def test_load_parses_name_and_geometry(tmp_path: Path):
    """PlayfieldDefinition.load sets name, width_cm, height_cm from the file."""
    path = _write_playfield(tmp_path, "main-playfield")
    defn = PlayfieldDefinition.load(path)

    assert defn.name == "main-playfield"
    assert defn.width_cm == 134.3
    assert defn.height_cm == 89.3
    assert defn.origin == "apriltag-center-a1"


def test_load_populates_marker_lists(tmp_path: Path):
    """All four marker category lists are loaded."""
    path = _write_playfield(tmp_path)
    defn = PlayfieldDefinition.load(path)

    assert len(defn.april_tags) == 1
    assert len(defn.aruco_tags) == 8
    assert len(defn.rectangles) == 1
    assert len(defn.dots) == 1


def test_load_display_name_defaults_to_name(tmp_path: Path):
    """display_name falls back to the filename stem when absent from JSON."""
    path = _write_playfield(tmp_path, "my-field")
    defn = PlayfieldDefinition.load(path)

    assert defn.display_name == "my-field"


def test_load_display_name_from_json(tmp_path: Path):
    """display_name is read from playfield.display_name when present."""
    data = {**_MAIN_PLAYFIELD_DATA,
            "playfield": {**_MAIN_PLAYFIELD_DATA["playfield"], "display_name": "Main Field"}}
    path = _write_playfield(tmp_path, "main-playfield", data)
    defn = PlayfieldDefinition.load(path)

    assert defn.display_name == "Main Field"


def test_load_raises_file_not_found(tmp_path: Path):
    """load raises FileNotFoundError for a non-existent path."""
    with pytest.raises(FileNotFoundError):
        PlayfieldDefinition.load(tmp_path / "missing.json")


def test_load_raises_value_error_for_bad_json(tmp_path: Path):
    """load raises ValueError for a file that is not valid JSON."""
    bad = tmp_path / "bad.json"
    bad.write_text("this is not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        PlayfieldDefinition.load(bad)


def test_load_raises_value_error_missing_geometry(tmp_path: Path):
    """load raises ValueError when width_cm or height_cm is absent."""
    data = {"playfield": {"origin": "x"}, "april_tags": [], "aruco_tags": [],
            "rectangles": [], "dots": []}
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="geometry"):
        PlayfieldDefinition.load(path)


# ---------------------------------------------------------------------------
# corner_aruco_ids
# ---------------------------------------------------------------------------


def test_corner_aruco_ids(tmp_path: Path):
    """corner_aruco_ids() returns [1, 3, 5, 7] — NW, NE, SE, SW order from JSON."""
    path = _write_playfield(tmp_path)
    defn = PlayfieldDefinition.load(path)
    ids = defn.corner_aruco_ids()

    # Must include all four corner IDs
    assert set(ids) == {1, 3, 5, 7}
    # Ordering must match the JSON order: NW=1, NE=3, SE=5, SW=7
    assert ids == [1, 3, 5, 7]


def test_corner_aruco_ids_excludes_non_corner_cardinals(tmp_path: Path):
    """Non-diagonal markers (north, south, east, west, center) are excluded."""
    path = _write_playfield(tmp_path)
    defn = PlayfieldDefinition.load(path)
    ids = defn.corner_aruco_ids()

    # IDs 2 (north), 4 (east), 6 (south), 8 (west) must NOT appear
    for excluded in [2, 4, 6, 8]:
        assert excluded not in ids


# ---------------------------------------------------------------------------
# corner_world_coords
# ---------------------------------------------------------------------------


def test_corner_world_coords(tmp_path: Path):
    """corner_world_coords() returns positions matching the acceptance criteria.

    For the fixture (NW, NE, SE, SW JSON order) this must yield exactly
    [(-67, 44.65), (67, 44.65), (67, -44.65), (-67, -44.65)].
    """
    path = _write_playfield(tmp_path)
    defn = PlayfieldDefinition.load(path)
    coords = defn.corner_world_coords()

    # Exact ordered list from the ticket acceptance criteria
    assert coords == [(-67.0, 44.65), (67.0, 44.65), (67.0, -44.65), (-67.0, -44.65)]


def test_corner_world_coords_same_order_as_ids(tmp_path: Path):
    """corner_world_coords() and corner_aruco_ids() have matching order."""
    path = _write_playfield(tmp_path)
    defn = PlayfieldDefinition.load(path)
    ids = defn.corner_aruco_ids()
    coords = defn.corner_world_coords()

    assert len(ids) == len(coords)
    # Verify each coord matches the tag for that id in aruco_tags
    id_to_coord = {int(t["id"]): (float(t["x"]), float(t["y"]))
                   for t in defn.aruco_tags}
    for tag_id, coord in zip(ids, coords):
        assert coord == id_to_coord[tag_id]


# ---------------------------------------------------------------------------
# PlayfieldDefinitionRegistry.load_all
# ---------------------------------------------------------------------------


def test_registry_load_all_two_files(tmp_path: Path):
    """load_all with two fixture files → registry has both names."""
    _write_playfield(tmp_path, "alpha-field")
    _write_playfield(tmp_path, "beta-field")

    reg = PlayfieldDefinitionRegistry()
    reg.load_all(tmp_path)

    names = reg.list()
    assert "alpha-field" in names
    assert "beta-field" in names
    assert len(names) == 2


def test_registry_missing_dir(tmp_path: Path):
    """load_all on a non-existent directory → empty registry, no exception."""
    reg = PlayfieldDefinitionRegistry()
    reg.load_all(tmp_path / "does-not-exist")  # must not raise

    assert reg.list() == []
    assert reg.first() is None


def test_registry_malformed_json_skipped(tmp_path: Path):
    """A malformed JSON file is skipped; the valid one is still loaded."""
    _write_playfield(tmp_path, "good-field")
    (tmp_path / "bad-field.json").write_text("this is not json", encoding="utf-8")

    reg = PlayfieldDefinitionRegistry()
    reg.load_all(tmp_path)

    assert reg.list() == ["good-field"]


def test_registry_get_known_name(tmp_path: Path):
    """get() returns the correct PlayfieldDefinition for a known name."""
    _write_playfield(tmp_path, "main-playfield")
    reg = PlayfieldDefinitionRegistry()
    reg.load_all(tmp_path)

    defn = reg.get("main-playfield")
    assert defn.name == "main-playfield"
    assert defn.width_cm == 134.3


def test_registry_get_unknown_raises_key_error(tmp_path: Path):
    """get() raises KeyError for an unknown name."""
    reg = PlayfieldDefinitionRegistry()
    with pytest.raises(KeyError):
        reg.get("nonexistent")


def test_registry_first_returns_none_when_empty():
    """first() returns None when the registry is empty."""
    reg = PlayfieldDefinitionRegistry()
    assert reg.first() is None


def test_registry_first_returns_alphabetically_first(tmp_path: Path):
    """first() returns the alphabetically first definition."""
    _write_playfield(tmp_path, "zebra-field")
    _write_playfield(tmp_path, "alpha-field")

    reg = PlayfieldDefinitionRegistry()
    reg.load_all(tmp_path)

    first = reg.first()
    assert first is not None
    assert first.name == "alpha-field"


# ---------------------------------------------------------------------------
# mcp_server: module-level singleton
# ---------------------------------------------------------------------------


def test_mcp_server_has_playfield_def_registry():
    """mcp_server exposes a module-level PlayfieldDefinitionRegistry singleton."""
    import aprilcam.server.mcp_server as mcp

    assert hasattr(mcp, "playfield_def_registry")
    assert isinstance(mcp.playfield_def_registry, PlayfieldDefinitionRegistry)


# ---------------------------------------------------------------------------
# _handle_where: registry path and fallback path
# ---------------------------------------------------------------------------


def test_handle_where_fallback_to_legacy_playfield(monkeypatch, tmp_path: Path):
    """_handle_where falls back to playfield.json when the registry is empty."""
    from aprilcam.config import Config
    import aprilcam.server.mcp_server as mcp

    data_dir = tmp_path / "d"
    data_dir.mkdir()
    (data_dir / "playfield.json").write_text(json.dumps({
        "playfield": {"width_cm": 134.3, "height_cm": 89.3, "origin": "x"},
        "april_tags": [],
        "aruco_tags": [],
        "rectangles": [
            {"slug": "rect-east-red", "type": "rectangle", "color": "red",
             "cardinal": "east", "x": 35, "y": 0, "width_cm": 5, "height_cm": 4},
        ],
        "dots": [],
    }), encoding="utf-8")
    (tmp_path / "s").mkdir(exist_ok=True)
    fake_cfg = Config(data_dir=data_dir, socket_dir=tmp_path / "s",
                      daemon_pidfile=tmp_path / "s" / "p.pid")
    monkeypatch.setattr(mcp.Config, "load", classmethod(lambda cls, *a, **k: fake_cfg))

    # Ensure the module-level registry is empty for this test
    original_defs = dict(mcp.playfield_def_registry._defs)
    mcp.playfield_def_registry._defs.clear()
    try:
        result = mcp._handle_where("where is the eastern red square")
        assert result["status"] == "ok"
        assert result["matches"][0]["slug"] == "rect-east-red"
    finally:
        mcp.playfield_def_registry._defs.update(original_defs)


def test_handle_where_uses_registry_when_populated(monkeypatch, tmp_path: Path):
    """_handle_where uses the registry def when it is populated."""
    import aprilcam.server.mcp_server as mcp

    # Write a fixture playfield and load it into the module-level registry
    playfields_dir = tmp_path / "playfields"
    playfields_dir.mkdir()
    _write_playfield(playfields_dir, "main-playfield")

    original_defs = dict(mcp.playfield_def_registry._defs)
    mcp.playfield_def_registry._defs.clear()
    mcp.playfield_def_registry.load_all(playfields_dir)
    try:
        result = mcp._handle_where("where is the eastern red rectangle")
        assert result["status"] == "ok"
        assert result["matches"][0]["slug"] == "rect-east-red"
    finally:
        mcp.playfield_def_registry._defs.clear()
        mcp.playfield_def_registry._defs.update(original_defs)
