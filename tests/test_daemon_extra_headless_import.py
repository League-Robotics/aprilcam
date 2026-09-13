"""Headless-import safety proof for the ``daemon`` extra (sprint 040
ticket 002).

``plan-daemon-runs-on-a-remote-raspberry-pi-...md`` named two concrete
deployment risks to check before a live Pi install is attempted. This
module is the mechanical proof for the first one: **``mss`` (a
screen-capture library that needs X11 on Linux, listed in
``pyproject.toml``'s ``daemon`` extra) must never be imported as a
side effect of starting the daemon**, or ``python -m aprilcam.daemon``
would crash on first import on a headless Pi with no X server.

A grep alone (``tests/test_no_cv2_in_client_modules.py``'s AST-scan
style) would only prove no file under ``src/aprilcam/`` has a literal
``import mss``/``from mss`` -- it says nothing about a *transitive*
import reached through a third-party dependency's own import graph.
This test instead reproduces ``tests/test_cv2_import_blocker_console_
script.py``'s ``sys.meta_path`` blocker technique, but inverted: make
``mss`` genuinely unimportable, then import the exact module graph
``python -m aprilcam.daemon`` executes (``daemon.__main__``, which
transitively imports ``camera``, ``config``, ``daemon.rpc``,
``daemon.service``, and everything they depend on) and assert it still
succeeds. If a future change reintroduces a top-level ``import mss``
anywhere in that graph, this test fails immediately, without needing
real Pi hardware or an absent ``$DISPLAY`` to surface it.

Audit finding (2026-09-13, sprint 040 ticket 002): a full grep of
``src/aprilcam/`` found zero references to ``mss`` anywhere -- this
was already true before this ticket (``camera/backends/__init__.py``'s
own module docstring: "Screen capture is not ported this ticket ...
deferred, no stub file"). This test turns that snapshot into a
mechanically enforced regression gate rather than leaving it as a
one-time grep result. No source change was needed: the risk was
already moot, and this test proves it rather than merely asserting it.

The second named risk (``opencv-contrib-python`` aarch64/cp312 wheel
availability) is not testable offline -- it was verified instead by
building the real wheel (``python -m build --wheel``) and ``pip
install``-ing ``aprilcam[daemon]`` from it into a throwaway venv on
``vidar.local`` (Ubuntu 24.04 aarch64, Python 3.12.3), confirmed by an
SSH session recorded in this ticket's file, not by any test here.
"""

from __future__ import annotations

import sys
from typing import Iterable

import pytest


class _BlockMssImports:
    """A ``sys.meta_path`` finder that makes ``mss``/``mss.*`` genuinely
    unimportable, reproducing a headless Linux host with no X11/mss
    dependency chain satisfied -- same technique as ``tests/test_cv2_
    import_blocker_console_script.py``'s ``_BlockCv2Imports``."""

    def find_spec(self, fullname, path, target=None):  # noqa: ANN001, ANN201
        if fullname == "mss" or fullname.startswith("mss."):
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
def mss_blocked(monkeypatch: pytest.MonkeyPatch):
    """Installs :class:`_BlockMssImports` ahead of the real finders for
    the duration of one test. ``monkeypatch`` restores ``sys.meta_path``
    on teardown; callers are responsible for their own
    :func:`_evict_modules` call, since eviction scope is test-specific."""
    monkeypatch.setattr(sys, "meta_path", [_BlockMssImports(), *sys.meta_path])
    yield


def test_mss_blocker_actually_blocks() -> None:
    """Sanity check on the blocker mechanism itself, run with no
    fixture so ``mss`` really is importable outside this test (it is a
    real dev-environment dependency, part of the ``daemon`` extra) --
    confirms the blocker below would actually catch a regression rather
    than being silently inert."""
    monkeypatch_meta_path = [_BlockMssImports(), *sys.meta_path]
    original = sys.meta_path
    sys.meta_path = monkeypatch_meta_path
    try:
        _evict_modules(["mss"])
        with pytest.raises(ModuleNotFoundError):
            import mss  # noqa: F401
    finally:
        sys.meta_path = original


def test_daemon_main_module_imports_with_mss_blocked(mss_blocked: None) -> None:
    """The load-bearing proof: ``aprilcam.daemon.__main__`` -- the exact
    module ``python -m aprilcam.daemon`` executes -- imports cleanly
    with ``mss`` genuinely unimportable. This is the module graph a
    real headless Pi boot exercises before ``main()`` ever runs, not
    just ``aprilcam.daemon``'s package ``__init__``."""
    _evict_modules(
        [
            "aprilcam.daemon",
            "aprilcam.camera",
            "aprilcam.config",
            "aprilcam.vision",
            "aprilcam.calibration",
            "aprilcam.playfield",
            "mss",
        ]
    )

    import aprilcam.daemon.__main__  # noqa: F401


def test_mss_not_imported_as_side_effect_of_daemon_main_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Complementary, non-blocking proof: even with ``mss`` genuinely
    installed and importable (the normal dev/daemon-extra environment),
    importing ``aprilcam.daemon.__main__`` never causes ``mss`` to be
    imported as a side effect. Catches the same regression as the
    blocked-import test above from the opposite direction -- a module
    that wraps its own ``import mss`` in a broad ``try/except
    ImportError`` would still pass the blocked-import test (the
    exception is swallowed) but would show up here."""
    _evict_modules(
        [
            "aprilcam.daemon",
            "aprilcam.camera",
            "aprilcam.config",
            "aprilcam.vision",
            "aprilcam.calibration",
            "aprilcam.playfield",
            "mss",
        ]
    )

    import aprilcam.daemon.__main__  # noqa: F401

    assert "mss" not in sys.modules, (
        "aprilcam.daemon.__main__ must never import mss as a side "
        "effect -- it is an X11-only screen-capture dependency that "
        "would break a headless Pi boot"
    )
