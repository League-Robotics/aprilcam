"""Static type checking gate: ``mypy`` over ``src/aprilcam``.

See ``pyproject.toml``'s ``[tool.mypy]`` section for scope and the
strictness rationale (pragmatic, not ``--strict`` -- unstubbed
third-party libraries and the generated protobuf modules are handled
via targeted per-module overrides there, not a blanket
``ignore_missing_imports``).

Runs ``mypy`` as a subprocess with no extra arguments, so it reads
``pyproject.toml`` exactly the way ``uv run mypy`` does by hand --
this test and the documented command can never silently drift apart.
Equivalent to (and the same thing to run standalone while iterating):

    uv run mypy
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip(
    "mypy",
    reason=(
        "mypy is a dev dependency (dependency-groups.dev in "
        "pyproject.toml); run `uv sync` (or `uv sync --group dev`) to "
        "install it"
    ),
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_mypy_passes_over_src_aprilcam() -> None:
    """The whole ``src/aprilcam`` tree type-checks cleanly.

    A failure here means either a genuine type error was introduced, or
    a new third-party import needs a targeted ``[[tool.mypy.overrides]]``
    entry in ``pyproject.toml`` (see that section's docstring-comment
    for why the ignore list is per-module, not global).
    """
    result = subprocess.run(
        [sys.executable, "-m", "mypy"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, (
        "mypy reported issues over src/aprilcam -- run `uv run mypy` "
        "locally to reproduce:\n\n" + result.stdout + result.stderr
    )
