"""Static discipline gate: nothing under ``src/aprilcam/`` or ``tests/``
imports the archived v1 tree, or a v1-era module name Stage 1's
demolition condemned by name (ticket 031-011, SUC-011).

``docs/design/development-plan.md``'s Stage 1 moved the entire v1 tree
to ``archive/`` rather than deleting it outright, specifically so it
stays *readable* as an ideas source for the convert-don't-rewrite work
(``CLAUDE.md``'s archive-mining rule: "go read how v1 did it... before
designing any of it from scratch") without sitting in the v2 build.
Stage 12 ("the package restructure... last, when the code that moves
is final") named the closing check this test is: confirm the
incremental restructure sprints 026-030 actually performed (``server/``
-> ``mcp/``, ``core/`` dissolved, ``view/`` extracted, ``ui/`` deleted)
left nothing importing a module that move condemned. ``archive/``
itself is excluded from test collection (``pyproject.toml``'s
``norecursedirs``), so this is the only mechanical check that the
archived tree stays a read-only reference and never becomes a runtime
dependency.

An AST scan, same shape as ``tests/test_no_cv2_in_client_modules.py``'s
cv2 gate: it catches a forbidden import even if it is never reached at
runtime by whatever a given test session happens to exercise, and
needs no daemon, camera, or display. Two things are checked, each
across both ``src/aprilcam/`` and ``tests/``:

1. **No import of ``archive`` itself** -- every reference to
   ``archive/`` actually found in ``src/aprilcam/`` and ``tests/`` (as
   of this sweep, 2026-08-21) is prose in a docstring or comment citing
   where v1 code was converted from (e.g. "Converted from
   ``archive/src/aprilcam/camera/registry.py``"), never a live
   ``import archive`` / ``from archive import ...`` statement. This
   test makes that distinction mechanical rather than a claim someone
   has to re-verify by re-grepping.
2. **No import of a specific condemned v1-era dotted name** -- the old
   top-level ``core``/``server``/``ui`` packages (dissolved, renamed to
   ``mcp``, and deleted respectively, per Stage 12's own description)
   and the old ``daemon.protocol``/``daemon.client`` modules
   (superseded by ``proto/`` + ``client/daemon_client.py``'s gRPC
   surface). ``ls src/aprilcam/`` already shows only clean v2 names
   (``calibration/``, ``camera/``, ``cli/``, ``client/``, ``config/``,
   ``daemon/``, ``mcp/``, ``playfield/``, ``proto/``, ``tags/``,
   ``view/``, ``vision/``) -- this half of the sweep is what confirms
   nothing still *imports* the old names by a stale relative or
   string-built path, which a directory listing alone cannot rule out.

Both checks came back clean on this sweep -- see ticket 031-011's
completion notes for the full record.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Callable, Iterator, List, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src" / "aprilcam"
TESTS_ROOT = REPO_ROOT / "tests"

#: Dotted module names Stage 1's "Delete" list condemned by name and
#: Stage 12's restructure confirms are gone: the old top-level
#: ``core``/``server``/``ui`` packages, and the old
#: ``daemon.protocol``/``daemon.client`` modules superseded by
#: ``proto/`` + ``client/daemon_client.py``. A match on the exact name
#: or a dotted prefix of it (e.g. ``aprilcam.core.detector``) both
#: count as an offense.
_CONDEMNED_MODULES = (
    "aprilcam.core",
    "aprilcam.server",
    "aprilcam.ui",
    "aprilcam.daemon.protocol",
    "aprilcam.daemon.client",
    "aprilcam.stream",
)


def _python_files(root: Path) -> Iterator[Path]:
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


def _imported_names(path: Path) -> List[str]:
    """Every module name *path* imports, exactly as it names it (e.g.
    ``"archive"``, ``"aprilcam.core.detector"``)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    names: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def _offenders(root: Path, is_forbidden: Callable[[str], bool]) -> List[Tuple[str, List[str]]]:
    offenders = []
    for path in _python_files(root):
        found = [name for name in _imported_names(path) if is_forbidden(name)]
        if found:
            offenders.append((str(path.relative_to(REPO_ROOT)), found))
    return offenders


def _imports_archive(name: str) -> bool:
    return name == "archive" or name.startswith("archive.")


def _imports_condemned_module(name: str) -> bool:
    return any(name == condemned or name.startswith(condemned + ".") for condemned in _CONDEMNED_MODULES)


@pytest.mark.parametrize("root", [SRC_ROOT, TESTS_ROOT], ids=["src/aprilcam", "tests"])
def test_no_module_imports_archive(root: Path) -> None:
    offenders = _offenders(root, _imports_archive)
    assert not offenders, (
        "no module under src/aprilcam/ or tests/ may import archive/ "
        "(SUC-011 / ticket 031-011) -- archive/ is a read-only v1 "
        "ideas source (CLAUDE.md's archive-mining rule), never a "
        "runtime dependency, but found:\n"
        + "\n".join(f"  {file}: {names}" for file, names in offenders)
    )


@pytest.mark.parametrize("root", [SRC_ROOT, TESTS_ROOT], ids=["src/aprilcam", "tests"])
def test_no_module_imports_a_condemned_v1_module_name(root: Path) -> None:
    offenders = _offenders(root, _imports_condemned_module)
    assert not offenders, (
        "no module under src/aprilcam/ or tests/ may import a v1-era "
        "module name Stage 1's demolition condemned by name and Stage "
        "12's restructure confirmed removed (aprilcam.core/server/ui, "
        "aprilcam.daemon.protocol/client, top-level aprilcam.stream), "
        "but found:\n" + "\n".join(f"  {file}: {names}" for file, names in offenders)
    )
