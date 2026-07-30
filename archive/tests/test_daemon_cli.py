"""Tests for daemon_cli and view_cli import cleanliness.

Verifies:
  - daemon_cli.py contains no imports from aprilcam.daemon.client
  - daemon_cli.py contains no direct socket.socket() calls
  - view_cli.py contains no imports from aprilcam.daemon.client
  - view_cli.py contains no socket.socket() calls
  - DaemonControl is imported (not ControlClient)
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SRC_ROOT = Path(__file__).parent.parent / "src" / "aprilcam" / "cli"


def _read_source(filename: str) -> str:
    return (_SRC_ROOT / filename).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# daemon_cli.py import checks
# ---------------------------------------------------------------------------


class TestDaemonCliImports:
    """Verify daemon_cli.py uses DaemonControl, not the old client."""

    def test_no_import_from_daemon_client(self):
        src = _read_source("daemon_cli.py")
        assert "from aprilcam.daemon.client" not in src
        assert "from ..daemon.client" not in src

    def test_no_direct_socket_socket(self):
        src = _read_source("daemon_cli.py")
        # Must not create raw sockets directly
        assert "socket.socket(" not in src

    def test_imports_daemon_control(self):
        src = _read_source("daemon_cli.py")
        assert "DaemonControl" in src

    def test_no_control_client_reference(self):
        src = _read_source("daemon_cli.py")
        assert "ControlClient" not in src

    def test_no_ensure_running_reference(self):
        src = _read_source("daemon_cli.py")
        assert "ensure_running" not in src


# ---------------------------------------------------------------------------
# view_cli.py import checks
# ---------------------------------------------------------------------------


class TestViewCliImports:
    """Verify view_cli.py uses stream consumers, not raw socket code."""

    def test_no_import_from_daemon_client(self):
        src = _read_source("view_cli.py")
        assert "from aprilcam.daemon.client" not in src
        assert "from ..daemon.client" not in src

    def test_no_import_from_daemon_protocol(self):
        src = _read_source("view_cli.py")
        assert "from aprilcam.daemon.protocol" not in src
        assert "from ..daemon.protocol" not in src

    def test_no_direct_socket_socket(self):
        src = _read_source("view_cli.py")
        # Must not create raw sockets directly (only stream consumers do that internally)
        assert "socket.socket(" not in src

    def test_imports_daemon_control(self):
        src = _read_source("view_cli.py")
        assert "DaemonControl" in src

    def test_no_read_frame_reference(self):
        src = _read_source("view_cli.py")
        # read_frame was the old msgpack-based reader
        assert "read_frame(" not in src

    def test_imports_connect_default(self):
        src = _read_source("view_cli.py")
        assert "connect_default" in src


# ---------------------------------------------------------------------------
# Module import check
# ---------------------------------------------------------------------------


class TestCliModuleImports:
    """Both CLI modules must be importable without errors."""

    def test_daemon_cli_importable(self):
        # Fresh import
        mod_name = "aprilcam.cli.daemon_cli"
        sys.modules.pop(mod_name, None)
        mod = importlib.import_module(mod_name)
        assert hasattr(mod, "main")

    def test_view_cli_importable(self):
        mod_name = "aprilcam.cli.view_cli"
        sys.modules.pop(mod_name, None)
        mod = importlib.import_module(mod_name)
        assert hasattr(mod, "main")


# ---------------------------------------------------------------------------
# _tag_record_to_dict conversion
# ---------------------------------------------------------------------------


class TestTagRecordToDict:
    """view_cli._tag_record_to_dict converts TagRecord fields correctly."""

    def test_basic_conversion(self):
        from aprilcam.cli.view_cli import _tag_record_to_dict
        from aprilcam.client.models import TagRecord

        tr = TagRecord(
            id=42,
            center_px=(100.0, 200.0),
            corners_px=[(90.0, 190.0), (110.0, 190.0), (110.0, 210.0), (90.0, 210.0)],
            yaw=1.57,
            world_xy=(30.0, 50.0),
            in_playfield=True,
            vel_px=(5.0, -3.0),
            speed_px=5.83,
            vel_world=(2.0, -1.5),
            speed_world=2.5,
            heading_rad=0.5,
            age=0.1,
        )
        d = _tag_record_to_dict(tr)

        assert d["id"] == 42
        assert d["center_px"] == [100.0, 200.0]
        assert d["orientation_yaw"] == 1.57
        assert d["world_xy"] == [30.0, 50.0]
        assert d["in_playfield"] is True
        assert d["vel_px"] == [5.0, -3.0]
        assert d["vel_world"] == [2.0, -1.5]
        assert len(d["corners_px"]) == 4

    def test_none_world_xy(self):
        from aprilcam.cli.view_cli import _tag_record_to_dict
        from aprilcam.client.models import TagRecord

        tr = TagRecord(
            id=1,
            center_px=(50.0, 60.0),
            corners_px=[(40.0, 50.0), (60.0, 50.0), (60.0, 70.0), (40.0, 70.0)],
            yaw=0.0,
            world_xy=None,
            in_playfield=False,
            vel_px=None,
            speed_px=None,
            vel_world=None,
            speed_world=None,
            heading_rad=None,
            age=0.0,
        )
        d = _tag_record_to_dict(tr)

        assert d["world_xy"] is None
        assert d["vel_px"] == [0.0, 0.0]
        assert d["vel_world"] is None
