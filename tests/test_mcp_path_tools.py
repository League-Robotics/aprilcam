"""Unit tests for the MCP path tools added in T002.

Tests cover all four handlers (_handle_create_path, _handle_delete_path,
_handle_list_paths, _handle_clear_paths) using mocked/replaced registries
so that no real camera hardware is required.

Each test isolates the handler under test by replacing the module-level
registries with fresh instances (or mocks) and restoring them in a fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("aprilcam.daemon.grpc_server", reason="requires aprilcam[daemon]")

from aprilcam.server import mcp_server
from aprilcam.server.mcp_server import (
    _handle_create_path,
    _handle_delete_path,
    _handle_list_paths,
    _handle_clear_paths,
    PlayfieldRegistry,
    PlayfieldEntry,
    path_registry as _real_path_registry,
)
from aprilcam.server.paths import PathRegistry, Waypoint
from aprilcam.core.playfield import PlayfieldBoundary as Playfield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_WAYPOINT = {
    "x": 10.0,
    "y": 20.0,
    "size_cm": 3.0,
    "symbol": "circle",
    "symbol_color": [255, 0, 0],
    "line_color": [0, 255, 0],
}


def _make_waypoint_json(*overrides: dict) -> str:
    """Return a JSON string with one waypoint, optionally overriding fields."""
    wp = dict(VALID_WAYPOINT)
    for ov in overrides:
        wp.update(ov)
    return json.dumps([wp])


# ---------------------------------------------------------------------------
# Fixtures — replace module-level registries for each test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_registries(monkeypatch):
    """Give each test a fresh playfield_registry and path_registry."""
    fresh_playfield_registry = PlayfieldRegistry()
    fresh_path_registry = PathRegistry()
    monkeypatch.setattr(mcp_server, "playfield_registry", fresh_playfield_registry)
    monkeypatch.setattr(mcp_server, "path_registry", fresh_path_registry)
    yield fresh_playfield_registry, fresh_path_registry


def _register_playfield(playfield_registry: PlayfieldRegistry, playfield_id: str = "pf_cam_0") -> str:
    """Insert a minimal PlayfieldEntry into playfield_registry and return its id."""
    # We need a Playfield object; create a stub with just enough for the registry
    # PlayfieldEntry only needs playfield_id and camera_id for our handler logic.
    entry = PlayfieldEntry(
        playfield_id=playfield_id,
        camera_id="cam_0",
        playfield=None,  # type: ignore[arg-type]  # not used by path tools
    )
    playfield_registry.register(entry)
    return playfield_id


# ---------------------------------------------------------------------------
# create_path tests
# ---------------------------------------------------------------------------


def test_create_path_unknown_playfield(isolated_registries):
    """Error returned when playfield_id is not registered."""
    result = _handle_create_path("pf_unknown", _make_waypoint_json())
    assert result == {"error": "Unknown playfield_id 'pf_unknown'"}


def test_create_path_invalid_json(isolated_registries):
    """Error returned when waypoints_json cannot be parsed as JSON."""
    pf_reg, _ = isolated_registries
    _register_playfield(pf_reg)
    result = _handle_create_path("pf_cam_0", "not json")
    # Exact message: prefix is fixed; suffix comes from json.JSONDecodeError for this specific input.
    assert result == {"error": "Invalid waypoints JSON: Expecting value: line 1 column 1 (char 0)"}


def test_create_path_empty_list(isolated_registries):
    """Error returned when waypoints_json is an empty list."""
    pf_reg, _ = isolated_registries
    _register_playfield(pf_reg)
    result = _handle_create_path("pf_cam_0", "[]")
    assert result == {"error": "waypoints must be a non-empty list"}


def test_create_path_invalid_symbol(isolated_registries):
    """Error returned with exact message when symbol is not in VALID_SYMBOLS."""
    pf_reg, _ = isolated_registries
    _register_playfield(pf_reg)
    result = _handle_create_path("pf_cam_0", _make_waypoint_json({"symbol": "hexagon"}))
    assert result == {"error": "Invalid symbol 'hexagon'"}


def test_create_path_negative_size_cm(isolated_registries):
    """Error returned with exact message when size_cm is not positive."""
    pf_reg, _ = isolated_registries
    _register_playfield(pf_reg)
    result = _handle_create_path("pf_cam_0", _make_waypoint_json({"size_cm": -1.0}))
    assert result == {"error": "size_cm must be positive"}


def test_create_path_size_cm_zero(isolated_registries):
    """Error returned when size_cm is zero (must be > 0)."""
    pf_reg, _ = isolated_registries
    _register_playfield(pf_reg)
    result = _handle_create_path("pf_cam_0", _make_waypoint_json({"size_cm": 0.0}))
    assert result == {"error": "size_cm must be positive"}


def test_create_path_color_out_of_range(isolated_registries):
    """Error returned when a color channel value is outside [0, 255]."""
    pf_reg, _ = isolated_registries
    _register_playfield(pf_reg)
    result = _handle_create_path(
        "pf_cam_0",
        _make_waypoint_json({"symbol_color": [256, 0, 0]}),
    )
    assert "error" in result
    assert "symbol_color" in result["error"]


def test_create_path_success(isolated_registries):
    """Valid input produces path_000 and stores the path in path_registry."""
    pf_reg, path_reg = isolated_registries
    _register_playfield(pf_reg)
    result = _handle_create_path("pf_cam_0", _make_waypoint_json())
    assert result == {"path_id": "path_000"}
    # Verify the path was actually stored
    assert path_reg.get("path_000") is not None


# ---------------------------------------------------------------------------
# delete_path tests
# ---------------------------------------------------------------------------


def test_delete_path_known(isolated_registries):
    """Deleting an existing path returns confirmed deletion."""
    pf_reg, path_reg = isolated_registries
    _register_playfield(pf_reg)
    # Create a path first
    _handle_create_path("pf_cam_0", _make_waypoint_json())
    result = _handle_delete_path("path_000")
    assert result == {"deleted": True, "path_id": "path_000"}
    # Verify it's gone
    assert path_reg.get("path_000") is None


def test_delete_path_unknown(isolated_registries):
    """Deleting a non-existent path returns the specified error message."""
    result = _handle_delete_path("path_999")
    assert result == {"error": "Unknown path_id 'path_999'"}


# ---------------------------------------------------------------------------
# list_paths tests
# ---------------------------------------------------------------------------


def test_list_paths_empty(isolated_registries):
    """Listing paths for a playfield with no paths returns an empty list."""
    pf_reg, _ = isolated_registries
    _register_playfield(pf_reg)
    result = _handle_list_paths("pf_cam_0")
    assert result == {"playfield_id": "pf_cam_0", "paths": []}


def test_list_paths_with_entries(isolated_registries):
    """Listing paths returns all paths belonging to the playfield."""
    pf_reg, _ = isolated_registries
    _register_playfield(pf_reg)
    _handle_create_path("pf_cam_0", _make_waypoint_json())
    _handle_create_path("pf_cam_0", _make_waypoint_json({"x": 50.0}))
    result = _handle_list_paths("pf_cam_0")
    assert result["playfield_id"] == "pf_cam_0"
    assert len(result["paths"]) == 2
    path_ids = [p["path_id"] for p in result["paths"]]
    assert "path_000" in path_ids
    assert "path_001" in path_ids


def test_list_paths_unknown_playfield(isolated_registries):
    """list_paths returns error when playfield is not registered."""
    result = _handle_list_paths("pf_unknown")
    assert result == {"error": "Unknown playfield_id 'pf_unknown'"}


# ---------------------------------------------------------------------------
# clear_paths tests
# ---------------------------------------------------------------------------


def test_clear_paths(isolated_registries):
    """clear_paths removes all paths for the playfield and returns their ids."""
    pf_reg, path_reg = isolated_registries
    _register_playfield(pf_reg)
    _handle_create_path("pf_cam_0", _make_waypoint_json())
    _handle_create_path("pf_cam_0", _make_waypoint_json({"x": 50.0}))
    result = _handle_clear_paths("pf_cam_0")
    assert "cleared" in result
    assert set(result["cleared"]) == {"path_000", "path_001"}
    # Verify all paths are gone
    assert len(path_reg) == 0


def test_clear_paths_unknown_playfield(isolated_registries):
    """clear_paths returns error when playfield is not registered."""
    result = _handle_clear_paths("pf_unknown")
    assert result == {"error": "Unknown playfield_id 'pf_unknown'"}


# ---------------------------------------------------------------------------
# paths.json write tests
# ---------------------------------------------------------------------------


def test_path_tools_push_paths_rpc(isolated_registries, monkeypatch):
    """After path mutations, _push_paths_rpc is called with the playfield's camera_id.

    Now that the MCP server persists path state via the SetPaths gRPC RPC
    (not local file writes), this test mocks _push_paths_rpc and verifies
    it is called with the correct camera_id for create, delete, and clear.
    """
    pf_reg, path_reg = isolated_registries

    # Track _push_paths_rpc calls.
    push_calls: list[str] = []

    def _fake_push_paths_rpc(playfield_id: str) -> None:
        push_calls.append(playfield_id)

    monkeypatch.setattr(mcp_server, "_push_paths_rpc", _fake_push_paths_rpc)

    # Register the playfield.
    entry = PlayfieldEntry(
        playfield_id="pf_cam_0",
        camera_id="cam_0",
        playfield=None,  # type: ignore[arg-type]
    )
    pf_reg.register(entry)

    # create_path should call _push_paths_rpc once.
    push_calls.clear()
    result = _handle_create_path("pf_cam_0", _make_waypoint_json())
    assert "path_id" in result
    assert push_calls == ["pf_cam_0"], "create_path must call _push_paths_rpc"

    # delete_path should call _push_paths_rpc once.
    push_calls.clear()
    _handle_delete_path("path_000")
    assert push_calls == ["pf_cam_0"], "delete_path must call _push_paths_rpc"

    # clear_paths should call _push_paths_rpc once.
    _handle_create_path("pf_cam_0", _make_waypoint_json())
    _handle_create_path("pf_cam_0", _make_waypoint_json({"x": 50.0}))
    push_calls.clear()
    _handle_clear_paths("pf_cam_0")
    assert push_calls == ["pf_cam_0"], "clear_paths must call _push_paths_rpc"
