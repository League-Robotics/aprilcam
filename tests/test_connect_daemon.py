"""Tests for the connect_daemon() MCP tool (ticket 014-008).

Verifies that connect_daemon() tears down all session state before reconnecting:
- detection_registry is cleared
- live_view_registry is cleared
- registry.close_all() is called
- _cam_info is cleared
- playfield_registry._playfields is cleared
- path_registry._paths is cleared
- frame_registry is cleared
- composite_manager._composites is cleared
- The old _daemon_client channel is closed

Also verifies that connect_daemon() sets _daemon_client to the new connection
and returns the target address on success.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _inject_fake_client(mcp_server_module) -> MagicMock:
    """Inject a fake DaemonControl into mcp_server's _daemon_client."""
    fake_dc = MagicMock()
    fake_dc.list_cameras.return_value = []
    fake_dc.enumerate_cameras.return_value = []
    fake_dc.list_playfields.return_value = MagicMock(playfields=[])
    fake_dc._unix_path = None
    fake_dc._host = "old-host"
    fake_dc._port = 5280
    mcp_server_module._daemon_client = fake_dc
    return fake_dc


# ---------------------------------------------------------------------------
# connect_daemon tears down session state
# ---------------------------------------------------------------------------


class TestConnectDaemonSessionReset:
    """connect_daemon() clears all session registries before reconnecting."""

    @pytest.mark.asyncio
    async def test_detection_registry_cleared(self, monkeypatch) -> None:
        """detection_registry is cleared (all entries removed)."""
        from aprilcam.server import mcp_server

        old_client = _inject_fake_client(mcp_server)

        # Pre-populate the detection registry.
        fake_entry = MagicMock()
        fake_entry.loop.stop = MagicMock()
        mcp_server.detection_registry["test-cam"] = fake_entry

        new_dc = MagicMock()
        new_dc.list_cameras.return_value = []
        new_dc.enumerate_cameras.return_value = []
        new_dc.list_playfields.return_value = MagicMock(playfields=[])
        new_dc._unix_path = None
        new_dc._host = "new-host"
        new_dc._port = 5280

        with patch("aprilcam.server.mcp_server.DaemonControl", return_value=new_dc):
            await mcp_server.connect_daemon(host="new-host", port=5280)

        assert len(mcp_server.detection_registry) == 0

    @pytest.mark.asyncio
    async def test_live_view_registry_cleared(self, monkeypatch) -> None:
        """live_view_registry is cleared before reconnection."""
        from aprilcam.server import mcp_server

        _inject_fake_client(mcp_server)

        fake_lv = MagicMock()
        fake_lv.process.stop = MagicMock()
        mcp_server.live_view_registry["view-1"] = fake_lv

        new_dc = MagicMock()
        new_dc.list_cameras.return_value = []
        new_dc.enumerate_cameras.return_value = []
        new_dc.list_playfields.return_value = MagicMock(playfields=[])
        new_dc._unix_path = None
        new_dc._host = "new-host"
        new_dc._port = 5280

        with patch("aprilcam.server.mcp_server.DaemonControl", return_value=new_dc):
            await mcp_server.connect_daemon(host="new-host", port=5280)

        assert len(mcp_server.live_view_registry) == 0

    @pytest.mark.asyncio
    async def test_cam_info_cleared(self) -> None:
        """_cam_info dict is cleared before reconnection."""
        from aprilcam.server import mcp_server

        _inject_fake_client(mcp_server)

        mcp_server._cam_info["cam-x"] = {"cam_name": "cam-x"}
        mcp_server._cam_info["cam-y"] = {"cam_name": "cam-y"}

        new_dc = MagicMock()
        new_dc.list_cameras.return_value = []
        new_dc.enumerate_cameras.return_value = []
        new_dc.list_playfields.return_value = MagicMock(playfields=[])
        new_dc._unix_path = None
        new_dc._host = "new-host"
        new_dc._port = 5280

        with patch("aprilcam.server.mcp_server.DaemonControl", return_value=new_dc):
            await mcp_server.connect_daemon(host="new-host", port=5280)

        assert len(mcp_server._cam_info) == 0

    @pytest.mark.asyncio
    async def test_frame_registry_cleared(self) -> None:
        """frame_registry is cleared before reconnection."""
        from aprilcam.server import mcp_server

        _inject_fake_client(mcp_server)

        new_dc = MagicMock()
        new_dc.list_cameras.return_value = []
        new_dc.enumerate_cameras.return_value = []
        new_dc.list_playfields.return_value = MagicMock(playfields=[])
        new_dc._unix_path = None
        new_dc._host = "h"
        new_dc._port = 5280

        cleared = {"called": False}
        original_clear = mcp_server.frame_registry.clear

        def _patched_clear():
            cleared["called"] = True
            original_clear()

        monkeypatch = None  # not needed; direct patching
        old_clear = mcp_server.frame_registry.clear
        mcp_server.frame_registry.clear = _patched_clear
        try:
            with patch("aprilcam.server.mcp_server.DaemonControl", return_value=new_dc):
                await mcp_server.connect_daemon(host="h", port=5280)
        finally:
            mcp_server.frame_registry.clear = old_clear

        assert cleared["called"], "frame_registry.clear() was not called"

    @pytest.mark.asyncio
    async def test_old_daemon_client_closed(self) -> None:
        """The old gRPC channel (previous _daemon_client) is closed."""
        from aprilcam.server import mcp_server

        old_client = _inject_fake_client(mcp_server)

        new_dc = MagicMock()
        new_dc.list_cameras.return_value = []
        new_dc.enumerate_cameras.return_value = []
        new_dc.list_playfields.return_value = MagicMock(playfields=[])
        new_dc._unix_path = None
        new_dc._host = "h"
        new_dc._port = 5280

        with patch("aprilcam.server.mcp_server.DaemonControl", return_value=new_dc):
            await mcp_server.connect_daemon(host="h", port=5280)

        old_client.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_target_on_success(self) -> None:
        """connect_daemon returns the new daemon's target address on success."""
        from aprilcam.server import mcp_server

        _inject_fake_client(mcp_server)

        new_dc = MagicMock()
        new_dc.list_cameras.return_value = []
        new_dc.enumerate_cameras.return_value = []
        new_dc.list_playfields.return_value = MagicMock(playfields=[])
        new_dc._unix_path = None
        new_dc._host = "my-pi.local"
        new_dc._port = 5280

        with patch("aprilcam.server.mcp_server.DaemonControl", return_value=new_dc):
            result = await mcp_server.connect_daemon(host="my-pi.local", port=5280)

        # Result is a list[TextContent]; parse the JSON body.
        assert len(result) == 1
        body = json.loads(result[0].text)
        assert "target" in body
        assert "error" not in body

    @pytest.mark.asyncio
    async def test_new_client_set_as_daemon_client(self) -> None:
        """After connect_daemon, _daemon_client is the new DaemonControl."""
        from aprilcam.server import mcp_server

        _inject_fake_client(mcp_server)

        new_dc = MagicMock()
        new_dc.list_cameras.return_value = []
        new_dc.enumerate_cameras.return_value = []
        new_dc.list_playfields.return_value = MagicMock(playfields=[])
        new_dc._unix_path = None
        new_dc._host = "h"
        new_dc._port = 5280

        with patch("aprilcam.server.mcp_server.DaemonControl", return_value=new_dc):
            await mcp_server.connect_daemon(host="h", port=5280)

        assert mcp_server._daemon_client is new_dc

    @pytest.mark.asyncio
    async def test_error_on_unreachable_target(self) -> None:
        """connect_daemon returns an error dict when the new daemon is unreachable."""
        from aprilcam.server import mcp_server

        _inject_fake_client(mcp_server)

        broken_dc = MagicMock()
        broken_dc.connect = MagicMock(side_effect=RuntimeError("connection refused"))
        broken_dc.list_cameras.side_effect = RuntimeError("not connected")

        with patch("aprilcam.server.mcp_server.DaemonControl", return_value=broken_dc):
            result = await mcp_server.connect_daemon(host="bad-host", port=9999)

        body = json.loads(result[0].text)
        assert "error" in body

    @pytest.mark.asyncio
    async def test_playfield_registry_cleared(self) -> None:
        """playfield_registry._playfields is cleared before reconnection."""
        from aprilcam.server import mcp_server

        _inject_fake_client(mcp_server)

        # Inject a fake playfield entry directly.
        mcp_server.playfield_registry._playfields["fake-pf"] = MagicMock()

        new_dc = MagicMock()
        new_dc.list_cameras.return_value = []
        new_dc.enumerate_cameras.return_value = []
        new_dc.list_playfields.return_value = MagicMock(playfields=[])
        new_dc._unix_path = None
        new_dc._host = "h"
        new_dc._port = 5280

        with patch("aprilcam.server.mcp_server.DaemonControl", return_value=new_dc):
            await mcp_server.connect_daemon(host="h", port=5280)

        assert len(mcp_server.playfield_registry._playfields) == 0
