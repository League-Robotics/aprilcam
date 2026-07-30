"""Regression guard: client-side modules must import cleanly with cv2 TRULY absent.

This test suite launches a FRESH subprocess per module under test, installs a
``sys.meta_path`` finder that raises ``ModuleNotFoundError`` for any ``cv2``
import (using the modern ``find_spec`` API, which Python 3.12+ requires), and
asserts the module loads successfully.

Unlike the existing tests in ``test_015_006_packaging_opencv_free.py`` and
``test_015_opencv_free_clients.py`` — which used the old ``find_module``/
``load_module`` meta-path API that Python 3.12+ silently ignores, and/or the
``builtins.__import__`` monkeypatching approach that is unreliable when
``aprilcam`` is already partially imported in the test process — this file
uses the **only** approach that genuinely works on Python 3.12+:

    1. Fresh ``sys.executable -c "..."`` subprocess (no shared module cache).
    2. ``find_spec``-based blocker installed before ANY aprilcam import.

This test **fails against the pre-fix code** (``core/__init__.py`` and
``camera/__init__.py`` used to eagerly import cv2-requiring submodules) and
**passes after the fix** (those imports are now lazy via ``__getattr__``).

Modules verified:
    - aprilcam.cli
    - aprilcam.cli.cameras_cli
    - aprilcam.cli.tags_cli
    - aprilcam.cli.view_cli
    - aprilcam.server.mcp_server
    - aprilcam.server.web_server
    - aprilcam.client.control
    - aprilcam.client.stream
    - aprilcam.client.discovery
"""

from __future__ import annotations

import subprocess
import sys

import pytest

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

_BLOCKER_PREAMBLE = """\
import sys

class _BlockCv2:
    \"\"\"Modern (find_spec) meta-path finder that blocks cv2 imports.\"\"\"
    @classmethod
    def find_spec(cls, name, path=None, target=None):
        if name == 'cv2' or name.startswith('cv2.'):
            raise ModuleNotFoundError(f'cv2 truly absent (blocked by test): {name}')
        return None

# Install BEFORE any aprilcam import so no transitive cv2 sneaks through
sys.meta_path.insert(0, _BlockCv2())
"""


def _assert_imports_without_cv2(module_name: str) -> None:
    """Launch a subprocess that blocks cv2 and then imports *module_name*.

    Asserts that the subprocess exits with code 0 (import succeeded).
    On failure, includes full stdout and stderr in the assertion message.
    """
    script = _BLOCKER_PREAMBLE + (
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
            f"Importing {module_name!r} failed when cv2 is absent "
            f"(subprocess exit {result.returncode}).\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


# ---------------------------------------------------------------------------
# Parametrized test — one subprocess per module
# ---------------------------------------------------------------------------

_OPENCV_FREE_MODULES = [
    "aprilcam.cli",
    "aprilcam.cli.cameras_cli",
    "aprilcam.cli.tags_cli",
    "aprilcam.cli.view_cli",
    "aprilcam.server.mcp_server",
    "aprilcam.server.web_server",
    "aprilcam.client.control",
    "aprilcam.client.stream",
    "aprilcam.client.discovery",
    "aprilcam.client.host_codes",
]


@pytest.mark.parametrize("module_name", _OPENCV_FREE_MODULES)
def test_module_imports_without_cv2(module_name: str) -> None:
    """Verify *module_name* imports cleanly in a subprocess with cv2 blocked.

    Uses ``find_spec``-based blocking (Python 3.12+ compatible) in a fresh
    process so the module cache is clean and the block cannot be bypassed by
    already-cached imports.
    """
    _assert_imports_without_cv2(module_name)
