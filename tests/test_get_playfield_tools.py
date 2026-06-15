"""Tests for the get_playfield / list_playfields MCP tools (OOP addition).

These exercise the handler logic against a monkeypatched module-level
``playfield_def_registry`` so they need no live camera or daemon.
"""
import json

from aprilcam.core.playfield_def import PlayfieldDefinitionRegistry
from aprilcam.server import mcp_server

_FIXTURE = {
    "playfield": {
        "width_cm": 134.3,
        "height_cm": 89.3,
        "origin": "apriltag-center-a1",
        "display_name": "Main Playfield",
    },
    "april_tags": [
        {"slug": "apriltag-center-a1", "type": "april_tag", "id": 1, "x": 0, "y": 0}
    ],
    "aruco_tags": [
        {"slug": "aruco-northwest-u1", "type": "aruco_tag", "id": 1,
         "cardinal": "northwest", "x": -67, "y": 44.65}
    ],
    "rectangles": [
        {"slug": "rect-east-red", "type": "rectangle", "color": "red", "x": 35, "y": 0}
    ],
    "dots": [
        {"slug": "dot-west-blue", "type": "dot", "color": "blue", "x": -50, "y": 0}
    ],
}


def _registry(tmp_path, name="main-playfield"):
    (tmp_path / f"{name}.json").write_text(json.dumps(_FIXTURE))
    reg = PlayfieldDefinitionRegistry()
    reg.load_all(tmp_path)
    return reg


def test_get_playfield_default_returns_whole_structure(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "playfield_def_registry", _registry(tmp_path))
    out = mcp_server._handle_get_playfield()
    assert "error" not in out
    assert out["name"] == "main-playfield"
    assert out["display_name"] == "Main Playfield"
    assert out["playfield"] == {
        "width_cm": 134.3, "height_cm": 89.3, "origin": "apriltag-center-a1"
    }
    # Every component category is present in full.
    assert len(out["april_tags"]) == 1
    assert len(out["aruco_tags"]) == 1
    assert out["rectangles"][0]["color"] == "red"
    assert out["dots"][0]["color"] == "blue"


def test_get_playfield_by_name(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "playfield_def_registry", _registry(tmp_path))
    out = mcp_server._handle_get_playfield("main-playfield")
    assert out["name"] == "main-playfield"


def test_get_playfield_unknown_name_lists_available(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "playfield_def_registry", _registry(tmp_path))
    out = mcp_server._handle_get_playfield("does-not-exist")
    assert "error" in out
    assert "main-playfield" in out["error"]


def test_list_playfields(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "playfield_def_registry", _registry(tmp_path))
    out = mcp_server._handle_list_playfields()
    assert out["playfields"] == [
        {"name": "main-playfield", "display_name": "Main Playfield",
         "width_cm": 134.3, "height_cm": 89.3}
    ]


def test_list_playfields_empty(monkeypatch):
    monkeypatch.setattr(mcp_server, "playfield_def_registry", PlayfieldDefinitionRegistry())
    assert mcp_server._handle_list_playfields() == {"playfields": []}
