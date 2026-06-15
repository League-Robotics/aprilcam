"""Tests for _resolve_source_playfield + the server golden-path instructions.

The resolver lets world-coordinate tools accept either a playfield_id or a
camera_id (auto-resolving the camera's associated playfield).
"""
from aprilcam.server import mcp_server
from aprilcam.server.mcp_server import PlayfieldEntry, PlayfieldRegistry


def _entry(playfield_id="pf_cam", camera_id="cam"):
    # _resolve_source_playfield only uses playfield_id/camera_id, so a plain
    # object() stands in for the Playfield.
    return PlayfieldEntry(playfield_id=playfield_id, camera_id=camera_id, playfield=object())


def test_resolve_by_playfield_id(monkeypatch):
    reg = PlayfieldRegistry()
    e = _entry()
    reg.register(e)
    monkeypatch.setattr(mcp_server, "playfield_registry", reg)
    assert mcp_server._resolve_source_playfield("pf_cam") is e


def test_resolve_by_camera_id(monkeypatch):
    reg = PlayfieldRegistry()
    e = _entry()
    reg.register(e)
    monkeypatch.setattr(mcp_server, "playfield_registry", reg)
    # Passing the camera handle still resolves the associated playfield.
    assert mcp_server._resolve_source_playfield("cam") is e


def test_resolve_via_explicit_camera_id_arg(monkeypatch):
    reg = PlayfieldRegistry()
    e = _entry()
    reg.register(e)
    monkeypatch.setattr(mcp_server, "playfield_registry", reg)
    # source_id is unknown, but the camera_id arg points at the playfield.
    assert mcp_server._resolve_source_playfield("something-else", camera_id="cam") is e


def test_resolve_unknown_returns_none(monkeypatch):
    monkeypatch.setattr(mcp_server, "playfield_registry", PlayfieldRegistry())
    assert mcp_server._resolve_source_playfield("nope") is None


def test_server_instructions_document_golden_path():
    instr = mcp_server._SERVER_INSTRUCTIONS
    assert "open_camera" in instr
    assert "playfield_id" in instr
    assert "world_xy" in instr
    # Instructions are attached to the running FastMCP server.
    assert getattr(mcp_server.server, "instructions", None)
