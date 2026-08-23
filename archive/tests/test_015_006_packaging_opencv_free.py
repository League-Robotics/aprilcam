"""Tests for ticket 015-006: OpenCV moved to daemon extra, Pillow to base.

Verifies:
1. pyproject.toml: opencv-contrib-python is NOT in base dependencies.
2. pyproject.toml: pillow>=10.0 IS in base dependencies.
3. cli/__init__.py: DAEMON_COMMANDS == frozenset({"daemon", "taggen", "calibrate"}).
4. stream.py ImageStreamConsumer.read() uses Pillow (no cv2) for JPEG decode.
5. mcp_server, web_server, view_cli, cameras_cli, and stream all import cleanly
   when cv2 is monkeypatched to be absent.
"""

from __future__ import annotations

import importlib
import io
import struct
import sys
from unittest.mock import MagicMock

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _block_cv2(original_import):
    """Return a replacement __import__ that raises ImportError for 'cv2'."""

    def _blocking_import(name, *args, **kwargs):
        if name == "cv2":
            raise ImportError("cv2 blocked by test (simulating base install)")
        return original_import(name, *args, **kwargs)

    return _blocking_import


def _stash_and_block_cv2():
    """Remove cv2 from sys.modules and return a context manager that blocks it."""
    import builtins

    original_import = builtins.__import__
    real_cv2 = sys.modules.pop("cv2", None)
    builtins.__import__ = _block_cv2(original_import)  # type: ignore[assignment]
    return original_import, real_cv2


def _restore_cv2(original_import, real_cv2):
    import builtins

    builtins.__import__ = original_import
    if real_cv2 is not None:
        sys.modules["cv2"] = real_cv2
    else:
        sys.modules.pop("cv2", None)


# ---------------------------------------------------------------------------
# pyproject.toml dependency checks
# ---------------------------------------------------------------------------


def test_pillow_in_base_dependencies():
    """pillow>=10.0 must be in the [project] dependencies list (base install)."""
    from pathlib import Path
    import re

    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    content = pyproject.read_text()

    # In TOML format, base dependencies are in [project] → dependencies = [...]
    # The block goes from "dependencies = [" to the closing "]"
    m = re.search(r'^dependencies\s*=\s*\[(.*?)\]', content, re.DOTALL | re.MULTILINE)
    assert m, "dependencies = [...] block not found in [project] section of pyproject.toml"
    deps_block = m.group(1)

    assert "pillow" in deps_block.lower(), (
        "pillow was not found in base dependencies; "
        "it must be a base dependency so the view client can decode images"
    )
    assert "opencv" not in deps_block.lower(), (
        "opencv-contrib-python must NOT appear in base dependencies; "
        "it belongs only in [project.optional-dependencies.daemon]"
    )


def test_opencv_only_in_daemon_extra():
    """opencv-contrib-python must appear ONLY under the daemon optional extra."""
    from pathlib import Path
    import re

    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    content = pyproject.read_text()

    # Find the [project.optional-dependencies] section
    m = re.search(
        r'\[project\.optional-dependencies\](.*?)(?=\n\[|\Z)',
        content, re.DOTALL
    )
    assert m, "[project.optional-dependencies] not found"
    opt_block = m.group(1)

    # Confirm opencv is present in daemon = [...]
    daemon_m = re.search(r'daemon\s*=\s*\[(.*?)\]', opt_block, re.DOTALL)
    assert daemon_m, "daemon extra not found in [project.optional-dependencies]"
    daemon_block = daemon_m.group(1)
    assert "opencv" in daemon_block.lower(), (
        "opencv-contrib-python must be listed in the daemon extra"
    )

    # Confirm opencv is NOT in the base dependencies block
    base_m = re.search(r'^dependencies\s*=\s*\[(.*?)\]', content, re.DOTALL | re.MULTILINE)
    assert base_m, "base dependencies block not found"
    assert "opencv" not in base_m.group(1).lower(), (
        "opencv-contrib-python must NOT be in base dependencies"
    )


# ---------------------------------------------------------------------------
# DAEMON_COMMANDS check
# ---------------------------------------------------------------------------


def test_daemon_commands_reduced():
    """DAEMON_COMMANDS must only contain the genuinely daemon-requiring commands."""
    from aprilcam.cli import DAEMON_COMMANDS

    assert DAEMON_COMMANDS == frozenset({"daemon", "taggen", "calibrate"}), (
        f"Expected frozenset({{'daemon', 'taggen', 'calibrate'}}), got {DAEMON_COMMANDS}"
    )


def test_opencv_free_commands_not_in_daemon_commands():
    """cameras, tags, view, mcp, web are opencv-free clients — not in DAEMON_COMMANDS."""
    from aprilcam.cli import DAEMON_COMMANDS

    thin_clients = {"cameras", "tags", "view", "mcp", "web"}
    overlap = thin_clients & DAEMON_COMMANDS
    assert not overlap, (
        f"These commands should NOT be in DAEMON_COMMANDS (they are opencv-free): {overlap}"
    )


# ---------------------------------------------------------------------------
# Import tests: modules must import without cv2
# ---------------------------------------------------------------------------


def _import_without_cv2(module_name: str) -> None:
    """Import *module_name* with cv2 blocked; raise on failure.

    Uses the same evict-then-restore pattern as test_015_005_view_cli_pillow.py.
    This pattern is safe only for modules WITHOUT module-level global state that
    other tests depend on (cameras_cli, view_cli, stream are safe).

    For modules with shared state (mcp_server, web_server), use
    ``_subprocess_import_without_cv2`` instead.
    """
    import builtins

    original_import = builtins.__import__
    real_cv2 = sys.modules.pop("cv2", None)
    cached_module = sys.modules.pop(module_name, None)

    builtins.__import__ = _block_cv2(original_import)  # type: ignore[assignment]
    try:
        importlib.import_module(module_name)
    finally:
        builtins.__import__ = original_import
        if real_cv2 is not None:
            sys.modules["cv2"] = real_cv2
        else:
            sys.modules.pop("cv2", None)
        sys.modules.pop(module_name, None)
        if cached_module is not None:
            sys.modules[module_name] = cached_module


def _subprocess_import_without_cv2(module_name: str) -> None:
    """Verify *module_name* imports without cv2 by running a subprocess.

    Launches a fresh Python interpreter with a sys.meta_path hook that blocks
    cv2.  Safe for modules with module-level shared state that cannot be
    evicted without polluting other tests.
    """
    import subprocess

    script = (
        "import sys\n"
        "class _BlockCv2:\n"
        "    def find_module(self, name, path=None): return self if name == 'cv2' else None\n"
        "    def load_module(self, name): raise ImportError('cv2 blocked')\n"
        "sys.meta_path.insert(0, _BlockCv2())\n"
        f"import {module_name}\n"
        f"print('{module_name} OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"Importing {module_name} without cv2 failed:\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )


def test_cameras_cli_imports_without_cv2():
    """aprilcam.cli.cameras_cli must import cleanly without cv2."""
    _import_without_cv2("aprilcam.cli.cameras_cli")


def test_mcp_server_imports_without_cv2():
    """aprilcam.server.mcp_server must import cleanly without cv2.

    Uses subprocess so that the module's shared global state (playfield_registry,
    etc.) is not disturbed — evicting and re-importing it would break other tests
    that rely on those globals.
    """
    _subprocess_import_without_cv2("aprilcam.server.mcp_server")


def test_web_server_imports_without_cv2():
    """aprilcam.server.web_server must import cleanly without cv2.

    Uses subprocess to avoid disturbing shared server state.
    """
    _subprocess_import_without_cv2("aprilcam.server.web_server")


def test_view_cli_imports_without_cv2():
    """aprilcam.cli.view_cli must import cleanly without cv2."""
    _import_without_cv2("aprilcam.cli.view_cli")


def test_stream_imports_without_cv2():
    """aprilcam.client.stream must import cleanly without cv2."""
    _import_without_cv2("aprilcam.client.stream")


# ---------------------------------------------------------------------------
# stream.py read() uses Pillow, not cv2
# ---------------------------------------------------------------------------


def _length_prefix(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def _build_image_frame_bytes(frame_id: int, jpeg: bytes) -> bytes:
    from aprilcam.proto import aprilcam_pb2

    msg = aprilcam_pb2.ImageFrame(
        frame_id=frame_id,
        ts_mono_ns=1_000_000,
        jpeg=jpeg,
        width=8,
        height=8,
    )
    return _length_prefix(msg.SerializeToString())


def test_image_stream_read_uses_pillow_not_cv2():
    """ImageStreamConsumer.read() must decode JPEG using Pillow, not cv2.

    Creates a tiny valid JPEG using Pillow itself, feeds it through the
    consumer, and verifies the result is an ndarray without cv2 being called.
    """
    import socket
    from PIL import Image
    from aprilcam.client.models import StreamEndpoint
    from aprilcam.client.stream import ImageStreamConsumer

    # Build a tiny 8x8 RGB JPEG via Pillow
    pil_img = Image.new("RGB", (8, 8), color=(100, 150, 200))
    buf = io.BytesIO()
    pil_img.save(buf, format="JPEG")
    jpeg = buf.getvalue()

    # Build a server/client socket pair
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)
    port = server_sock.getsockname()[1]

    client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_sock.connect(("127.0.0.1", port))
    conn, _ = server_sock.accept()
    server_sock.close()

    conn.sendall(_build_image_frame_bytes(frame_id=1, jpeg=jpeg))
    conn.close()

    endpoint = StreamEndpoint(tcp_port=None, socket_path=None)
    consumer = ImageStreamConsumer(endpoint)
    consumer._sock = client_sock

    # Block cv2 to confirm Pillow path is used
    original_import, real_cv2 = _stash_and_block_cv2()
    try:
        frame = consumer.read()
    finally:
        _restore_cv2(original_import, real_cv2)

    consumer.close()

    assert isinstance(frame, np.ndarray), "read() must return a numpy array"
    assert frame.shape == (8, 8, 3), f"Expected (8, 8, 3) RGB shape, got {frame.shape}"


def test_image_stream_read_returns_rgb():
    """ImageStreamConsumer.read() returns RGB (not BGR) after Pillow decode.

    Verifies the channel order matches Pillow's native RGB output — the
    channel ordering changed from BGR (cv2) to RGB (Pillow) in ticket 015-006.
    """
    import socket
    from PIL import Image
    from aprilcam.client.models import StreamEndpoint
    from aprilcam.client.stream import ImageStreamConsumer

    # Build an image with a distinctive colour in corner (0,0)
    pil_img = Image.new("RGB", (8, 8), color=(200, 100, 50))
    buf = io.BytesIO()
    pil_img.save(buf, format="JPEG", quality=95)
    jpeg = buf.getvalue()

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)
    port = server_sock.getsockname()[1]

    client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_sock.connect(("127.0.0.1", port))
    conn, _ = server_sock.accept()
    server_sock.close()

    conn.sendall(_build_image_frame_bytes(frame_id=2, jpeg=jpeg))
    conn.close()

    endpoint = StreamEndpoint(tcp_port=None, socket_path=None)
    consumer = ImageStreamConsumer(endpoint)
    consumer._sock = client_sock

    frame = consumer.read()
    consumer.close()

    # JPEG is lossy, so check channel ordering: R should be largest
    r, g, b = float(frame[0, 0, 0]), float(frame[0, 0, 1]), float(frame[0, 0, 2])
    assert r > g and r > b, (
        f"Expected red channel dominant (RGB order). Got R={r:.0f} G={g:.0f} B={b:.0f}. "
        "If B>R, cv2 BGR order is being used instead of Pillow RGB."
    )
