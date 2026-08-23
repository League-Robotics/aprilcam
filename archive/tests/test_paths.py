"""Unit tests for the paths data model and registry (T001).

Covers:
- Monotonic ID generation starting at path_000
- Delete known and unknown path_ids
- list_for filters by playfield_id
- clear_for removes all paths for a playfield and returns deleted ids
- to_dict round-trips cleanly through json.dumps / json.loads
"""

import json

import pytest

from aprilcam.server.paths import Path, PathRegistry, Waypoint, VALID_SYMBOLS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_waypoint(
    x: float = 10.0,
    y: float = 20.0,
    size_cm: float = 3.0,
    symbol: str = "circle",
    symbol_color=(255, 0, 0),
    line_color=(0, 255, 0),
) -> Waypoint:
    return Waypoint(
        x=x,
        y=y,
        size_cm=size_cm,
        symbol=symbol,
        symbol_color=symbol_color,
        line_color=line_color,
    )


# ---------------------------------------------------------------------------
# ID generation tests
# ---------------------------------------------------------------------------


def test_create_returns_path_with_monotonic_id():
    """First create yields path_000."""
    registry = PathRegistry()
    path = registry.create("pf_0", [make_waypoint()])
    assert path.path_id == "path_000"


def test_ids_are_monotonic():
    """Three creates yield path_000, path_001, path_002."""
    registry = PathRegistry()
    p0 = registry.create("pf_0", [make_waypoint()])
    p1 = registry.create("pf_0", [make_waypoint()])
    p2 = registry.create("pf_0", [make_waypoint()])
    assert p0.path_id == "path_000"
    assert p1.path_id == "path_001"
    assert p2.path_id == "path_002"


# ---------------------------------------------------------------------------
# Delete tests
# ---------------------------------------------------------------------------


def test_delete_known_id():
    """Create then delete; return value is the path; get returns None afterward."""
    registry = PathRegistry()
    path = registry.create("pf_0", [make_waypoint()])
    path_id = path.path_id

    deleted = registry.delete(path_id)
    assert deleted is path
    assert registry.get(path_id) is None


def test_delete_unknown_id_returns_none():
    """Deleting a nonexistent id returns None."""
    registry = PathRegistry()
    result = registry.delete("path_999")
    assert result is None


# ---------------------------------------------------------------------------
# list_for test
# ---------------------------------------------------------------------------


def test_list_for_filters_by_playfield():
    """Create paths for two different playfield ids; list_for on each returns only that playfield's paths."""
    registry = PathRegistry()
    p_a1 = registry.create("pf_A", [make_waypoint()])
    p_a2 = registry.create("pf_A", [make_waypoint()])
    p_b1 = registry.create("pf_B", [make_waypoint()])

    paths_a = registry.list_for("pf_A")
    paths_b = registry.list_for("pf_B")

    assert {p.path_id for p in paths_a} == {p_a1.path_id, p_a2.path_id}
    assert {p.path_id for p in paths_b} == {p_b1.path_id}


# ---------------------------------------------------------------------------
# clear_for test
# ---------------------------------------------------------------------------


def test_clear_for_removes_all_for_playfield():
    """Create two paths for one playfield, clear; list is empty; returned ids match."""
    registry = PathRegistry()
    p0 = registry.create("pf_X", [make_waypoint()])
    p1 = registry.create("pf_X", [make_waypoint()])
    registry.create("pf_Y", [make_waypoint()])  # should not be affected

    deleted_ids = registry.clear_for("pf_X")

    assert set(deleted_ids) == {p0.path_id, p1.path_id}
    assert registry.list_for("pf_X") == []
    # pf_Y path must survive
    assert len(registry.list_for("pf_Y")) == 1


# ---------------------------------------------------------------------------
# to_dict / JSON round-trip test
# ---------------------------------------------------------------------------


def test_to_dict_round_trip():
    """json.dumps(path.to_dict()) followed by json.loads reproduces all field values."""
    registry = PathRegistry()
    waypoints = [
        make_waypoint(x=5.0, y=10.0, size_cm=2.5, symbol="filled_square",
                      symbol_color=(255, 128, 0), line_color=(0, 0, 255)),
        make_waypoint(x=30.0, y=40.0, size_cm=4.0, symbol="x",
                      symbol_color=(0, 255, 255), line_color=(128, 128, 128)),
    ]
    path = registry.create("pf_rt", waypoints)

    serialised = json.dumps(path.to_dict())
    recovered = json.loads(serialised)

    assert recovered["path_id"] == path.path_id
    assert recovered["playfield_id"] == "pf_rt"
    assert len(recovered["waypoints"]) == 2

    wp0 = recovered["waypoints"][0]
    assert wp0["x"] == 5.0
    assert wp0["y"] == 10.0
    assert wp0["size_cm"] == 2.5
    assert wp0["symbol"] == "filled_square"
    assert wp0["symbol_color"] == [255, 128, 0]
    assert wp0["line_color"] == [0, 0, 255]

    wp1 = recovered["waypoints"][1]
    assert wp1["symbol"] == "x"
    assert wp1["symbol_color"] == [0, 255, 255]


# ---------------------------------------------------------------------------
# name field tests
# ---------------------------------------------------------------------------


def test_create_with_name_stores_name():
    """PathRegistry.create with name= stores that name on the returned Path."""
    registry = PathRegistry()
    path = registry.create("pf_0", [make_waypoint()], name="Test")
    assert path.name == "Test"


def test_create_without_name_defaults_to_empty_string():
    """PathRegistry.create without name= defaults to empty string."""
    registry = PathRegistry()
    path = registry.create("pf_0", [make_waypoint()])
    assert path.name == ""


def test_to_dict_includes_name_when_set():
    """to_dict() includes name key with correct value when name is set."""
    registry = PathRegistry()
    path = registry.create("pf_0", [make_waypoint()], name="Test")
    d = path.to_dict()
    assert d["name"] == "Test"


def test_to_dict_includes_name_when_default():
    """to_dict() includes name key with empty string when using default."""
    registry = PathRegistry()
    path = registry.create("pf_0", [make_waypoint()])
    d = path.to_dict()
    assert d["name"] == ""


def test_path_dataclass_name_field_is_optional():
    """Path can be constructed without name (backward compatibility)."""
    wp = make_waypoint()
    path = Path(path_id="path_000", playfield_id="pf_0", waypoints=[wp])
    assert path.name == ""


# ---------------------------------------------------------------------------
# VALID_SYMBOLS sanity check
# ---------------------------------------------------------------------------


def test_valid_symbols_has_exactly_eight_values():
    """VALID_SYMBOLS must contain exactly the 8 specified symbol strings."""
    expected = {
        "square", "filled_square",
        "circle", "filled_circle",
        "triangle", "filled_triangle",
        "x",
        "none",
    }
    assert VALID_SYMBOLS == expected
