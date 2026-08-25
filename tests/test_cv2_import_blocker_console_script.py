"""Import-blocker proof that the ``aprilcam`` console-script import
chain and ``client/renderer.py`` are ``cv2``-free (sprint 036 ticket
005).

``tests/test_no_cv2_in_client_modules.py``'s AST scan only catches a
module's own *literal* ``import cv2`` -- it cannot see a transitive
dependency reached through another module's import. This module
complements it: a real ``sys.meta_path`` finder makes ``cv2``/``cv2.*``
genuinely unimportable in-process (the same technique
``tests/aprilcam/cli/test_mcp_cli.py``'s ``_BlockMcpImports`` already
uses for the ``mcp`` SDK), so the assertion here is "this import
actually succeeds with cv2 gone," independent of whatever
``pyproject.toml`` currently declares as a base dependency -- unlike a
fresh-venv packaging test, this needs no wheel build and works even
while ``opencv-contrib-python`` is still a base dependency (see this
module's own tests for why that distinction matters right now).

**Why this is more than the two files ticket 005 directly touched.**
Ticket 005 splits ``calibration/geometry.py``'s two ``cv2``-dependent
functions into a new ``calibration/distortion.py``, fixing the
``cli/__init__.py`` -> ``cli/camera.py`` -> ``cli/locate.py`` ->
``calibration/geometry.py`` chain (and ``client/renderer.py``'s
identical dependency on ``geometry.py``) that ticket 004 discovered
breaks the *entire* console script in a base-only install. While
writing this proof, a **second**, independent top-level ``cv2`` import
was found in the same ``import aprilcam.cli`` graph:
``aprilcam.tags.generator`` (reached via ``cli/__init__.py`` ->
``cli/tags.py`` -> ``aprilcam.tags.generator``) also does ``import cv2
as cv`` at module scope. That module is explicitly out of this
ticket's scope (ticket 004's own stashed WIP already makes it lazy,
parked pending this ticket's landing per the sprint's ticket-004/005
resequencing) -- so ``import aprilcam.cli`` as a *whole* cannot
honestly be asserted to succeed with ``cv2`` blocked until ticket 004
also lands. This module is explicit about that split:

- :func:`test_calibration_geometry_imports_with_cv2_blocked` and
  :func:`test_client_renderer_imports_with_cv2_blocked` are direct,
  unconditional proof of this ticket's own change.
- :func:`test_cli_camera_and_locate_chain_imports_with_cv2_blocked`
  isolates and proves the exact chain this ticket is responsible for
  (``cli/camera.py``/``cli/locate.py``/``cli/config.py``/
  ``cli/daemon.py``/``cli/guide.py``/``cli/mcp.py``/``cli/playfield.py``,
  all of ``cli/__init__.py``'s eager submodule imports other than
  ``tags``) by stubbing out ``aprilcam.tags.generator`` -- the one
  piece this ticket does not touch -- rather than silently relying on
  it happening to already be fixed.
- :func:`test_aprilcam_cli_still_requires_cv2_via_tags_generator` is
  ``xfail(strict=True)``, mirroring
  ``tests/packaging/test_installed_package.py``'s own idiom for a
  known, tracked, not-yet-closed gap: it documents that the *whole*
  ``import aprilcam.cli`` does not yet succeed with ``cv2`` blocked,
  names the precise reason, and will flip to a hard failure (an
  unexpected pass) the day ticket 004 lands and closes the gap -- at
  which point this xfail marker (and, ideally, this module's docstring
  paragraph about the split) should be removed/updated.

**Eviction is required, not optional.** A module already cached in
``sys.modules`` from an earlier import elsewhere in this test session
is returned unchanged by a later ``import`` statement -- it is never
re-executed, so it would never actually exercise the ``cv2`` block.
Every test below evicts the ``aprilcam``/``cv2`` module prefixes it is
about to freshly re-import *before* installing the block, exactly like
``test_mcp_cli.py``'s ``mcp_sdk_uninstalled`` fixture does for ``mcp``/
``aprilcam.mcp``.
"""

from __future__ import annotations

import sys
import types
from typing import Iterable

import pytest


class _BlockCv2Imports:
    """A ``sys.meta_path`` finder that makes ``cv2``/``cv2.*`` genuinely
    unimportable, reproducing a base-only install's missing-``cv2``
    condition without uninstalling anything from this dev environment.
    Same technique as ``tests/aprilcam/cli/test_mcp_cli.py``'s
    ``_BlockMcpImports``."""

    def find_spec(self, fullname, path, target=None):  # noqa: ANN001, ANN201
        if fullname == "cv2" or fullname.startswith("cv2."):
            raise ModuleNotFoundError(
                f"No module named {fullname!r} (blocked for test)", name=fullname
            )
        return None


def _evict_modules(prefixes: Iterable[str]) -> None:
    """Remove every ``sys.modules`` entry equal to, or a dotted child
    of, any name in *prefixes* -- forces the next ``import`` of any of
    them to genuinely re-execute the module's top-level code rather
    than return an already-cached object."""
    prefixes = list(prefixes)
    for name in list(sys.modules):
        if name in prefixes or any(name.startswith(prefix + ".") for prefix in prefixes):
            del sys.modules[name]


@pytest.fixture()
def cv2_blocked(monkeypatch: pytest.MonkeyPatch):
    """Installs :class:`_BlockCv2Imports` ahead of the real finders for
    the duration of one test. ``monkeypatch`` restores ``sys.meta_path``
    on teardown; callers are responsible for their own
    :func:`_evict_modules` call (see module docstring) since eviction
    scope is test-specific and not something this fixture can guess."""
    monkeypatch.setattr(sys, "meta_path", [_BlockCv2Imports(), *sys.meta_path])
    yield


# ---------------------------------------------------------------------------
# Direct, unconditional proof of this ticket's own change.
# ---------------------------------------------------------------------------


def test_calibration_geometry_imports_with_cv2_blocked(cv2_blocked: None) -> None:
    """The load-bearing AC: ``calibration/geometry.py`` itself no
    longer imports ``cv2`` anywhere, mechanically, not by grep alone."""
    _evict_modules(["aprilcam.calibration", "cv2"])

    import aprilcam.calibration.geometry  # noqa: F401


def test_calibration_distortion_module_still_requires_cv2(cv2_blocked: None) -> None:
    """Sanity check on the blocker mechanism itself: ``distortion.py``
    -- the module the two ``cv2``-dependent functions moved *to* --
    must still fail to import with ``cv2`` blocked. If this test ever
    passes, the block installed by :func:`cv2_blocked` or the eviction
    in :func:`_evict_modules` is not actually working, which would mean
    every "succeeds with cv2 blocked" assertion in this module is a
    false positive."""
    _evict_modules(["aprilcam.calibration", "cv2"])

    with pytest.raises(ModuleNotFoundError):
        import aprilcam.calibration.distortion  # noqa: F401


def test_client_renderer_imports_with_cv2_blocked(cv2_blocked: None) -> None:
    """``client/renderer.py`` -- one of the two symptoms named in this
    ticket's own description -- imports cleanly with no ``cv2``
    anywhere in its transitive closure. Needs no stubbing: ``client/``
    has no dependency on ``aprilcam.tags`` at all."""
    _evict_modules(["aprilcam.client", "aprilcam.calibration", "cv2"])

    import aprilcam.client.renderer  # noqa: F401


# ---------------------------------------------------------------------------
# The console-script chain this ticket is responsible for, isolated from
# aprilcam.tags.generator's own separate, still-open cv2 import (ticket
# 004's territory -- see the module docstring).
# ---------------------------------------------------------------------------


def _stub_tags_generator(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replaces ``aprilcam.tags.generator`` in ``sys.modules`` with a
    bare stand-in exposing a placeholder ``TagGenerator`` name --
    enough for ``cli/tags.py``'s ``from aprilcam.tags.generator import
    TagGenerator`` to resolve without executing the real module's
    top-level ``import cv2 as cv`` (sprint 036 ticket 004's own,
    separate fix for that -- not this ticket's). ``aprilcam.tags``
    itself is imported for real first: its ``__init__.py`` is empty, so
    it is genuinely ``cv2``-free and needs no stubbing."""
    import aprilcam.tags

    stub = types.ModuleType("aprilcam.tags.generator")
    stub.TagGenerator = object  # placeholder -- nothing calls it during import
    monkeypatch.setitem(sys.modules, "aprilcam.tags.generator", stub)
    monkeypatch.setattr(aprilcam.tags, "generator", stub, raising=False)


def test_cli_camera_and_locate_chain_imports_with_cv2_blocked(
    cv2_blocked: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for exactly this ticket's own fix: with
    ``aprilcam.tags.generator`` stubbed out (see
    :func:`_stub_tags_generator`), ``import aprilcam.cli`` -- which
    eagerly imports ``camera`` (and, via ``camera``, ``calibrate`` and
    ``locate``), ``config``, ``daemon``, ``guide``, ``mcp``, and
    ``playfield`` -- succeeds with ``cv2`` genuinely blocked. This is
    the chain ticket 004's own exception named as breaking the whole
    console script; this ticket's ``geometry.py``/``distortion.py``
    split is what fixes it, proven here independently of the separate,
    still-open ``aprilcam.tags.generator`` gap."""
    _evict_modules(
        ["aprilcam.cli", "aprilcam.calibration", "aprilcam.client", "aprilcam.tags", "cv2"]
    )
    _stub_tags_generator(monkeypatch)

    import aprilcam.cli  # noqa: F401


# ---------------------------------------------------------------------------
# The known, separate, tracked gap -- xfail(strict=True) so it stays
# visible without blocking this ticket, and flips to a hard failure
# (not a silent pass) the day ticket 004 lands. Mirrors
# tests/packaging/test_installed_package.py's own idiom for exactly
# this situation.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason=(
        "known, separate packaging gap tracked for ticket 004 (sprint 036, "
        "same sprint as this ticket): aprilcam.tags.generator has its own "
        "top-level `import cv2 as cv`, unrelated to this ticket's "
        "calibration/geometry.py <-> distortion.py split, so `import "
        "aprilcam.cli` still pulls in cv2 via cli/tags.py -> "
        "aprilcam.tags.generator even after this ticket's fix. Confirmed "
        "NOT caused by the chain this ticket owns (cli/camera.py -> "
        "cli/locate.py -> calibration/geometry.py): see "
        "test_cli_camera_and_locate_chain_imports_with_cv2_blocked above, "
        "which isolates and proves that specific chain is clean by "
        "stubbing out aprilcam.tags.generator. Ticket 004's own stashed "
        "WIP on this branch (parked pending this ticket's landing, per "
        "the sprint's ticket-004/005 resequencing) already makes "
        "tags/generator.py's cv2 import lazy; this assertion should flip "
        "to an unexpected pass the day that fix lands, at which point "
        "this xfail marker must be removed."
    ),
    strict=True,
)
def test_aprilcam_cli_still_requires_cv2_via_tags_generator(cv2_blocked: None) -> None:
    _evict_modules(
        ["aprilcam.cli", "aprilcam.calibration", "aprilcam.client", "aprilcam.tags", "cv2"]
    )

    import aprilcam.cli  # noqa: F401
