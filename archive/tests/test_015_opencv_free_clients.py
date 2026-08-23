"""Regression guard (015-007): client-side modules must never import cv2.

This is the definitive opencv-free verification test for sprint 015.
It extends the import-checking coverage from test_015_006_packaging_opencv_free.py
(which covers cameras_cli, mcp_server, web_server, view_cli, and stream).

Additional coverage added here:
- aprilcam.cli (the top-level dispatcher)
- aprilcam.cli.tags_cli
- aprilcam.client.control
- aprilcam.client.discovery

Also asserts there are no top-level (module-scope) ``import cv2`` statements
in the four key server/client modules — the grep-style check is a belt-and-
suspenders guard that does not depend on live import machinery.
"""

from __future__ import annotations

import builtins
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers — same pattern as test_015_006 (subprocess for stateful modules)
# ---------------------------------------------------------------------------

_SRC_ROOT = Path(__file__).parent.parent / "src" / "aprilcam"


def _block_cv2_import(original_import):
    """Return a replacement __import__ that raises ImportError for cv2."""

    def _blocking(name, *args, **kwargs):
        if name == "cv2" or name.startswith("cv2."):
            raise ImportError(f"cv2 import blocked by test: {name!r}")
        return original_import(name, *args, **kwargs)

    return _blocking


def _import_without_cv2(module_name: str) -> None:
    """Import *module_name* in-process with cv2 blocked.

    Safe for stateless modules whose eviction and re-import won't pollute
    other tests.
    """
    original = builtins.__import__
    real_cv2 = sys.modules.pop("cv2", None)
    cached = sys.modules.pop(module_name, None)

    builtins.__import__ = _block_cv2_import(original)  # type: ignore[assignment]
    try:
        importlib.import_module(module_name)
    finally:
        builtins.__import__ = original
        if real_cv2 is not None:
            sys.modules["cv2"] = real_cv2
        else:
            sys.modules.pop("cv2", None)
        sys.modules.pop(module_name, None)
        if cached is not None:
            sys.modules[module_name] = cached


def _subprocess_import_without_cv2(module_name: str) -> None:
    """Verify *module_name* imports without cv2 via a subprocess.

    Used for modules with module-level shared state (mcp_server, web_server)
    whose eviction would corrupt other tests.
    """
    script = (
        "import sys\n"
        "class _BlockCv2:\n"
        "    def find_module(self, name, path=None):\n"
        "        return self if name == 'cv2' or name.startswith('cv2.') else None\n"
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
            f"Importing {module_name!r} without cv2 failed:\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# Import tests — modules new to this file (006 covers the rest)
# ---------------------------------------------------------------------------


def test_cli_dispatcher_imports_without_cv2():
    """aprilcam.cli (top-level dispatcher) must import cleanly without cv2.

    Uses subprocess to avoid evicting aprilcam.cli from sys.modules.  Submodules
    such as calibrate_cli and _daemon are registered as attributes on the parent
    package by other test modules at collection time; evicting and restoring the
    cached package object would drop those attributes and break tests that run
    afterward (e.g. test_camera_enum_selection.py patches aprilcam.cli._daemon).
    """
    _subprocess_import_without_cv2("aprilcam.cli")


def test_tags_cli_imports_without_cv2():
    """aprilcam.cli.tags_cli must import cleanly without cv2."""
    _import_without_cv2("aprilcam.cli.tags_cli")


def test_client_control_imports_without_cv2():
    """aprilcam.client.control must import cleanly without cv2."""
    _import_without_cv2("aprilcam.client.control")


def test_client_discovery_imports_without_cv2():
    """aprilcam.client.discovery must import cleanly without cv2."""
    _import_without_cv2("aprilcam.client.discovery")


# ---------------------------------------------------------------------------
# Grep-style checks: no top-level ``import cv2`` in server/client modules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel_path", [
    "server/mcp_server.py",
    "server/web_server.py",
    "cli/view_cli.py",
    "client/stream.py",
])
def test_no_toplevel_cv2_import(rel_path: str) -> None:
    """Verify no module-scope ``import cv2`` statement exists in *rel_path*.

    This is a static check that survives even if the monkeypatching approach
    above has blind spots (e.g. conditional imports at function scope are fine
    and intentional — this only blocks module-scope ones).
    """
    path = _SRC_ROOT / rel_path
    assert path.exists(), f"Expected source file not found: {path}"
    lines = path.read_text().splitlines()
    # Only flag unindented (module-scope) imports — lazy ``import cv2`` inside
    # functions (indented) are intentional guards via require_cv2().
    violations = [
        f"line {i + 1}: {line!r}"
        for i, line in enumerate(lines)
        if (line.startswith("import cv2") or line.startswith("from cv2"))
    ]
    assert not violations, (
        f"Top-level cv2 import found in {rel_path}:\n" + "\n".join(violations)
    )
