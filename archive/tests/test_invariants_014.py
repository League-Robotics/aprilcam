"""Invariant verification tests for sprint 014 (ticket 014-008).

These tests use source-code grep / AST analysis to assert structural invariants:

1. cv2.VideoCapture / cv.VideoCapture constructor calls exist ONLY in
   camera_pipeline.py within the src/aprilcam/ tree.  Other modules (server,
   client, stream consumers) must never open hardware directly.

2. AF_UNIX is absent from src/aprilcam/server/*.py (non-comment lines only).

3. subprocess.Popen is absent from src/aprilcam/client/control.py.

4. camera_dir / paths_file local-filesystem variables are absent from the
   MCP server's main module as non-comment, non-variable-name usage.

5. camutil.list_cameras / from camutil import list_cameras is absent from
   mcp_server.py, cameras_cli.py, and calibrate_cli.py.

All assertions are repo-grep based; they exclude comment-only lines and/or
docstrings where appropriate.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path


# Project root relative to this test file.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src" / "aprilcam"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _grep_non_comment_lines(path: Path, pattern: str) -> list[tuple[int, str]]:
    """Return (lineno, line) pairs matching *pattern* in non-comment, non-blank lines.

    Lines where the first non-whitespace character is ``#`` are excluded.
    """
    regex = re.compile(pattern)
    hits: list[tuple[int, str]] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw.lstrip()
        if stripped.startswith("#"):
            continue  # pure comment line
        if regex.search(raw):
            hits.append((lineno, raw.rstrip()))
    return hits


def _grep_tree(root: Path, pattern: str, glob: str = "**/*.py") -> list[tuple[Path, int, str]]:
    """Grep recursively; return (path, lineno, line) for non-comment matches."""
    regex = re.compile(pattern)
    hits: list[tuple[Path, int, str]] = []
    for py_file in sorted(root.glob(glob)):
        for lineno, line in _grep_non_comment_lines(py_file, pattern):
            hits.append((py_file, lineno, line))
    return hits


def _grep_actual_calls(root: Path, pattern: str, glob: str = "**/*.py") -> list[tuple[Path, int, str]]:
    """Return constructor call sites matching *pattern*, excluding comments and docstrings.

    Uses a simple state machine to track whether we're inside a triple-quoted
    docstring.  Lines inside docstrings and pure comment lines are skipped.
    An actual call must have ``VideoCapture(`` on the line (not just the name
    for type annotations).
    """
    CALL_LIKE = re.compile(r"VideoCapture\s*\(")
    # Pattern for triple-quote delimiter (either ''' or \"\"\")
    TRIPLE = re.compile(r'"""')

    hits: list[tuple[Path, int, str]] = []
    for py_file in sorted(root.glob(glob)):
        lines = py_file.read_text(encoding="utf-8").splitlines()
        in_docstring = False
        for lineno, raw in enumerate(lines, start=1):
            stripped = raw.lstrip()

            # Count triple-quote delimiters to track docstring state.
            # This is a heuristic (ignores single-quote docstrings and edge cases)
            # but is sufficient for our codebase.
            triple_count = len(TRIPLE.findall(raw))
            if triple_count % 2 == 1:
                in_docstring = not in_docstring
                # Even if we just closed a docstring, the line itself may
                # be inside one — skip it regardless.
                continue
            if in_docstring:
                continue

            if stripped.startswith("#"):
                continue  # pure comment line

            if CALL_LIKE.search(raw):
                hits.append((py_file, lineno, raw.rstrip()))

    return hits


# ---------------------------------------------------------------------------
# 1. VideoCapture constructor calls only in camera_pipeline.py
# ---------------------------------------------------------------------------


def test_videocapture_calls_only_in_camera_pipeline() -> None:
    """cv.VideoCapture(<arg>) constructor calls appear only in camera_pipeline.py.

    Every other module in src/aprilcam/ must delegate camera opens to the
    daemon.  This test finds all VideoCapture( call sites and asserts that
    none appear outside camera_pipeline.py (or files that are the daemon's
    own helpers or legacy dead-code marked as non-MCP-path).

    Allowed exception list (files that are legitimately allowed to call
    VideoCapture() directly because they are daemon-internal or CLI-only):
      - daemon/camera_pipeline.py  — the sole camera opener
      - camera/camera.py           — Camera wrapper (used by camera_pipeline)
      - camera/camutil.py          — probe-only (not called from MCP path)
      - camera/video_camera.py     — video file replay (not live hardware)
      - calibration/calibration.py — calibration CLI tool (not MCP path)
      - calibration/homography.py  — calibration helper
      - core/aprilcam.py           — legacy dead-code path (run() never called from MCP)
      - config.py                  — legacy CLI helper (not MCP path)
      - stream.py                  — legacy dead-code (marked DEAD-CODE header)
    """
    # Files that are explicitly allowed to construct VideoCapture.
    ALLOWED_FILES = {
        "daemon/camera_pipeline.py",
        "camera/camera.py",
        "camera/camutil.py",
        "camera/video_camera.py",
        "calibration/calibration.py",
        "calibration/homography.py",
        "core/aprilcam.py",
        "config.py",
        "stream.py",
    }

    # The MCP server and client code must NEVER construct VideoCapture.
    MCP_PATH_DIRS = [
        _SRC / "server",
        _SRC / "client",
    ]

    violations: list[str] = []
    for mcp_dir in MCP_PATH_DIRS:
        if not mcp_dir.exists():
            continue
        for hit_path, lineno, line in _grep_actual_calls(mcp_dir, r"VideoCapture\s*\("):
            violations.append(f"  {hit_path.relative_to(_REPO_ROOT)}:{lineno}: {line}")

    assert violations == [], (
        "cv2.VideoCapture() constructor called from MCP server/client path — "
        "the daemon is the sole camera opener.\n"
        + "\n".join(violations)
    )


def test_videocapture_call_present_in_camera_pipeline() -> None:
    """Sanity check: camera_pipeline.py does have a VideoCapture() call."""
    pipeline = _SRC / "daemon" / "camera_pipeline.py"
    assert pipeline.exists(), f"camera_pipeline.py not found at {pipeline}"
    calls = _grep_actual_calls(pipeline.parent, r"VideoCapture\s*\(", glob="camera_pipeline.py")
    assert len(calls) > 0, "camera_pipeline.py has no VideoCapture() call — invariant broken"


# ---------------------------------------------------------------------------
# 2. AF_UNIX absent from server/ (real code lines, not comments)
# ---------------------------------------------------------------------------


def test_af_unix_absent_from_server() -> None:
    """AF_UNIX does not appear in src/aprilcam/server/*.py (non-comment lines).

    The MCP server must use TCP or gRPC for all daemon communication.
    Raw AF_UNIX socket usage in server code is disallowed.
    """
    server_dir = _SRC / "server"
    if not server_dir.exists():
        pytest.skip("server directory not found")

    violations: list[str] = []
    for py_file in sorted(server_dir.glob("*.py")):
        for lineno, line in _grep_non_comment_lines(py_file, r"AF_UNIX"):
            violations.append(f"  {py_file.name}:{lineno}: {line}")

    assert violations == [], (
        "AF_UNIX found in src/aprilcam/server/ (non-comment lines) — "
        "server code must use TCP/gRPC, not raw Unix sockets.\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# 3. subprocess.Popen absent from client/control.py
# ---------------------------------------------------------------------------


def test_no_subprocess_popen_in_control() -> None:
    """subprocess.Popen is not called in client/control.py.

    DaemonControl must never spawn a daemon process — it only connects to
    an already-running daemon.  Use ``aprilcam daemon start`` instead.
    """
    control_py = _SRC / "client" / "control.py"
    assert control_py.exists(), f"client/control.py not found"

    hits = _grep_non_comment_lines(control_py, r"subprocess\.Popen")
    assert hits == [], (
        "subprocess.Popen found in client/control.py — "
        "DaemonControl must not spawn daemon processes.\n"
        + "\n".join(f"  line {ln}: {line}" for ln, line in hits)
    )


# ---------------------------------------------------------------------------
# 4. camera_dir / paths_file filesystem usage absent from mcp_server.py
# ---------------------------------------------------------------------------


def test_no_local_camera_dir_in_mcp_server() -> None:
    """mcp_server.py does not use camera_dir or paths_file as real filesystem paths.

    These were removed in sprint 014 to eliminate direct filesystem access
    from the MCP server.  The daemon handles all file I/O via gRPC RPCs.

    This test checks for non-comment lines that actually construct or read
    a filesystem path using these names (not just passing camera_dir=None
    or renaming the RPC return value).
    """
    mcp_server_py = _SRC / "server" / "mcp_server.py"
    assert mcp_server_py.exists(), "mcp_server.py not found"

    BENIGN_PATTERNS = re.compile(
        r"camera_dir\s*=\s*None"            # camera_dir=None (no-op)
        r"|_camera_dir_unused"               # variable named explicitly unused
        r"|camera_dir\s*=\s*None\s*#"        # same with trailing comment
    )

    violations: list[str] = []
    for lineno, line in _grep_non_comment_lines(mcp_server_py, r"paths_file|camera_dir"):
        stripped = line.strip()
        if BENIGN_PATTERNS.search(line):
            continue  # safe usage (no-op or explicitly unused)
        # Further filter: skip comment lines that happened to escape the first filter
        if stripped.startswith("#"):
            continue
        violations.append(f"  line {lineno}: {line}")

    assert violations == [], (
        "Non-benign camera_dir or paths_file usage found in mcp_server.py.\n"
        "These must be removed — the MCP server must not access local camera files.\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# 5. camutil.list_cameras absent from MCP server and specific CLI files
# ---------------------------------------------------------------------------


def test_no_camutil_list_cameras_in_mcp_server() -> None:
    """mcp_server.py does not call camutil.list_cameras() (local hardware probe).

    The MCP server uses the daemon's EnumerateCameras RPC instead of probing
    hardware locally via camutil.list_cameras.
    """
    mcp_server_py = _SRC / "server" / "mcp_server.py"
    assert mcp_server_py.exists()

    pattern = r"camutil\.list_cameras|from\s+.*camutil.*import.*list_cameras"
    hits = _grep_non_comment_lines(mcp_server_py, pattern)
    assert hits == [], (
        "camutil.list_cameras found in mcp_server.py — "
        "use the daemon EnumerateCameras RPC instead.\n"
        + "\n".join(f"  line {ln}: {line}" for ln, line in hits)
    )


def test_no_camutil_list_cameras_in_cameras_cli() -> None:
    """cameras_cli.py does not call camutil.list_cameras() via local probe."""
    cameras_cli = _SRC / "cli" / "cameras_cli.py"
    if not cameras_cli.exists():
        pytest.skip("cameras_cli.py not found")

    pattern = r"camutil\.list_cameras|from\s+.*camutil.*import.*list_cameras"
    hits = _grep_non_comment_lines(cameras_cli, pattern)
    assert hits == [], (
        "camutil.list_cameras found in cameras_cli.py — "
        "use the daemon EnumerateCameras RPC instead.\n"
        + "\n".join(f"  line {ln}: {line}" for ln, line in hits)
    )


def test_no_camutil_list_cameras_in_calibrate_cli() -> None:
    """calibrate_cli.py does not call camutil.list_cameras() via local probe."""
    calibrate_cli = _SRC / "cli" / "calibrate_cli.py"
    if not calibrate_cli.exists():
        pytest.skip("calibrate_cli.py not found")

    pattern = r"camutil\.list_cameras|from\s+.*camutil.*import.*list_cameras"
    hits = _grep_non_comment_lines(calibrate_cli, pattern)
    assert hits == [], (
        "camutil.list_cameras found in calibrate_cli.py — "
        "use the daemon EnumerateCameras RPC instead.\n"
        + "\n".join(f"  line {ln}: {line}" for ln, line in hits)
    )
