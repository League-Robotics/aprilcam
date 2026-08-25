"""Proves the base-install/cv2-free packaging promise for real, against
a genuinely built and pip-installed wheel -- not by convention, and not
by reading ``pyproject.toml`` (sprint 035, ticket 013).

Turns the 2026-08-23 "pipx daemon silently sees zero cameras" incident
(``docs/knowledge/daemon-restart-launches-pipx-build-no-cameras.md``)
into a permanent regression gate: that incident happened because a
pipx install's daemon subprocess had no capture backend. This module
does not reproduce that incident directly -- it instead nails down the
adjacent, load-bearing claim the project overview and
``docs/design/design.md`` §9 item 1 both make: a **base** install (no
``[daemon]`` extra) should need no ``cv2`` at all. That claim is
currently false (see below), and asserting it honestly, rather than
inferring it from the dependency list, is this module's whole job.

**Expected to fail, deliberately.** ``pyproject.toml`` lists
``opencv-contrib-python>=4.8`` as a **base** dependency (not gated
behind ``optional-dependencies.daemon``), a known, currently-open gap
recorded in ``docs/design/design.md`` §9 item 1. So
:func:`test_import_cv2_fails_in_the_fresh_base_only_venv` below is
marked ``xfail(strict=True)``: it is expected to fail *today* (a fresh
base-only install still gets ``cv2``), and ``strict=True`` means the
day that packaging gap closes, this same assertion flips to an
unexpected pass -- reported as a hard failure, not a silent skip --
which is exactly the "starkly visible until closed" behaviour the
ticket calls for. Nothing about the assertion itself is weakened to
make the suite green.

**Two-tier build**: a module-scoped :func:`built_wheel` fixture runs
``uv build --wheel`` once, and a module-scoped :func:`fresh_base_venv`
fixture builds one fresh venv and ``uv pip install``s that wheel with
**no extras** once -- shared by every test below rather than rebuilt
per test (each of those two steps is a real subprocess: a full build
and a real dependency resolution/install). Both live entirely under a
private ``/tmp`` root and are removed in the fixture's own ``finally``,
never left in the repo tree.

**The consumer script is a separate process, run with the fresh venv's
own ``python``, never imported into this test process.** This test
process is the dev venv, which already has ``cv2`` importable via the
``[daemon]`` extra -- an in-process import check of anything from the
fresh venv would prove nothing about what *that* venv can and cannot
import. The round-trip consumer script
(:data:`_CONSUMER_SCRIPT`) is written to a temp file and invoked via
``subprocess.run([str(venv_python), str(script_path)], ...)``.

**The private daemon it talks to is started from *this* (dev) venv,
in-process**, via ``aprilcam.daemon.__main__.build()`` -- identical to
``tests/aprilcam/client/test_public_api_system.py``'s (ticket 012)
``daemon_only_session`` fixture. That daemon process has ``cv2`` (this
test process's own venv does), which is fine and expected: this
module's cv2-free claim is about the **base-install client venv**, not
the daemon side, which the project overview always intended to keep
behind ``[daemon]``. The round trip never opens a camera -- playfield
push/pull, annotation put/list/clear, and ``get_guide`` are all the
daemon can serve with zero cameras attached (UC-01.S-2) -- so it is
safe to run in an agent session with no camera TCC grant (see
``docs/knowledge/daemon-camera-grant-and-detection-priming.md``).

**Isolation.** ``config_dir``/``state_dir`` are relocated via the flag
tier directly (``Directories(config_dir=..., state_dir=...)``) for the
in-process daemon side, and via ``APRILCAM_CONFIG_DIR``/
``APRILCAM_STATE_DIR`` env vars (the only tier a separate *subprocess*
can be handed) for the consumer-script side -- both pointed at the
exact same ``tmp_path`` roots so the two sides agree. ``socket_dir`` is
relocated via ``XDG_RUNTIME_DIR`` to a short, private ``/tmp`` root on
both sides (unix-socket ~103-char path limit), mirroring
``tests/aprilcam/client/test_lifecycle.py``'s and
``tests/aprilcam/cli/test_cli_integration.py``'s identical pattern.
Real ``~/.config/aprilcam``/``~/.local/share/aprilcam`` are never
touched -- nothing here constructs a ``Directories()`` with no
overrides, and no fixture below writes a ``camera_position``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Dict, Iterator, Optional

import pytest

from aprilcam.config.directories import Directories
from aprilcam.daemon import __main__ as daemon_main

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Generous but bounded -- a real ``uv build``/``uv venv``/``uv pip
#: install`` each take single-digit seconds on this machine once uv's
#: own package cache is warm (confirmed by hand before writing this
#: suite), but a cold cache or a slow network should fail loudly via
#: timeout rather than hang the suite forever.
_SUBPROCESS_TIMEOUT = 300.0

#: Consumer script executed by the **fresh, base-only venv's own
#: python** -- never imported into this test process. Deliberately
#: imports only ``aprilcam.client.discovery`` (for ``Discovery``),
#: ``aprilcam.config.loader`` (for ``PlayfieldDefinition`` -- the
#: concrete type ``set_playfield`` requires, per
#: ``client/daemon_client.py``'s own ``isinstance`` guard), and
#: ``aprilcam.types`` -- exactly the chain a prior investigation of
#: this ticket traced as genuinely cv2-free (``client/discovery.py`` ->
#: ``client/daemon_client.py`` / ``daemon/interfaces.py`` ->
#: ``calibration/interfaces.py`` / ``playfield/interfaces.py`` ->
#: ``config/loader.py`` -- confirmed again directly while writing this
#: module: none of those modules import ``cv2``). Deliberately never
#: imports ``aprilcam.client.renderer``, which transitively imports
#: ``cv2`` via ``calibration/geometry.py`` (design.md §9 item 1) -- that
#: module is not part of the "thin client" surface this test is
#: proving.
_CONSUMER_SCRIPT = textwrap.dedent(
    """\
    import sys

    from aprilcam.client.discovery import Discovery
    from aprilcam.config.loader import PlayfieldDefinition
    from aprilcam.types import Annotation, Marker, WorldPoint

    _PLAYFIELD = "packaging-013-field"


    def main() -> int:
        daemon = Discovery().connect()

        definition = PlayfieldDefinition(name=_PLAYFIELD, width_cm=200.0, height_cm=100.0)
        daemon.set_playfield(definition)
        fetched = daemon.get_playfield(_PLAYFIELD)
        assert fetched.name == _PLAYFIELD, f"got {fetched.name!r}"

        marker = Annotation(
            id="consumer-marker",
            layer="consumer",
            shape=Marker(at=WorldPoint(x=5.0, y=5.0), label="hi"),
        )
        daemon.put_annotation(_PLAYFIELD, marker)
        after_put = daemon.get_annotations(_PLAYFIELD)
        assert any(a.id == "consumer-marker" for a in after_put.annotations), after_put

        daemon.clear_annotations(_PLAYFIELD)
        after_clear = daemon.get_annotations(_PLAYFIELD)
        assert after_clear.annotations == (), after_clear

        guide = daemon.get_guide()
        assert isinstance(guide, str) and guide.strip(), repr(guide)

        print("CONSUMER_ROUND_TRIP_OK")
        return 0


    if __name__ == "__main__":
        sys.exit(main())
    """
)


def _run(
    cmd: list, *, cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, cwd=cwd, env=env, timeout=_SUBPROCESS_TIMEOUT
    )


def _short_tmp_dir(prefix: str) -> Path:
    """A short, top-level ``/tmp`` directory -- unix domain sockets have
    a ~103-character path limit, and pytest's ``tmp_path`` nests too
    deeply (matches every other hardware/integration test module's
    identically-named helper in this suite, e.g.
    ``tests/aprilcam/client/test_public_api_system.py``)."""
    return Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))


# ---------------------------------------------------------------------------
# Build + install, once per module -- shared read-only by every test below.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def built_wheel() -> Iterator[Path]:
    """``uv build --wheel`` into a private temp out-dir. Cleaned up
    after every test in this module has run."""
    out_dir = _short_tmp_dir("ac-013-dist-")
    try:
        result = _run(["uv", "build", "--wheel", "-o", str(out_dir)], cwd=str(REPO_ROOT))
        assert result.returncode == 0, (
            f"uv build --wheel failed (exit {result.returncode}):\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        wheels = sorted(out_dir.glob("aprilcam-*.whl"))
        assert len(wheels) == 1, f"expected exactly one built wheel in {out_dir}, found {wheels}"
        yield wheels[0]
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


@pytest.fixture(scope="module")
def fresh_base_venv(built_wheel: Path) -> Iterator[Path]:
    """A fresh venv with *only* the BASE ``aprilcam`` package installed
    from :func:`built_wheel` -- no ``[daemon]`` extra, no other extra.
    Yields the venv's root directory; ``bin/python``, ``bin/aprilcam``
    live under it."""
    venv_dir = _short_tmp_dir("ac-013-venv-")
    try:
        venv_result = _run(["uv", "venv", str(venv_dir)], cwd=str(REPO_ROOT))
        assert venv_result.returncode == 0, (
            f"uv venv failed (exit {venv_result.returncode}):\n"
            f"stdout={venv_result.stdout}\nstderr={venv_result.stderr}"
        )
        venv_python = venv_dir / "bin" / "python"
        install_result = _run(
            ["uv", "pip", "install", "--python", str(venv_python), str(built_wheel)],
            cwd=str(REPO_ROOT),
        )
        assert install_result.returncode == 0, (
            f"uv pip install (base only) failed (exit {install_result.returncode}):\n"
            f"stdout={install_result.stdout}\nstderr={install_result.stderr}"
        )
        yield venv_dir
    finally:
        shutil.rmtree(venv_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC1 -- build + fresh venv + base-only install actually happened.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_build_and_base_only_install_produce_a_working_venv(fresh_base_venv: Path):
    venv_python = fresh_base_venv / "bin" / "python"
    assert venv_python.exists(), f"expected a python binary at {venv_python}"

    result = _run([str(venv_python), "--version"])
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"


# ---------------------------------------------------------------------------
# AC2 -- `import aprilcam.client` succeeds in the fresh venv.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_import_aprilcam_client_succeeds_in_the_fresh_base_only_venv(fresh_base_venv: Path):
    venv_python = fresh_base_venv / "bin" / "python"
    result = _run([str(venv_python), "-c", "import aprilcam.client"])
    assert result.returncode == 0, (
        f"import aprilcam.client failed in a fresh base-only-install venv "
        f"(exit {result.returncode}):\nstdout={result.stdout}\nstderr={result.stderr}"
    )


@pytest.mark.integration
def test_import_aprilcam_client_renderer_succeeds_in_the_fresh_base_only_venv(
    fresh_base_venv: Path,
):
    """Sprint 036 ticket 005: ``client/renderer.py`` imports
    ``calibration/geometry.py`` for ``world_to_deskewed_pixel``, and
    ``geometry.py`` used to have its own top-level ``import cv2 as cv``
    (needed only by the two functions ticket 005 moved to
    ``calibration/distortion.py``). At the point this ticket lands,
    ``opencv-contrib-python`` is still a base dependency (ticket 004
    hasn't moved it to the ``daemon`` extra yet), so this assertion
    passes trivially either way today -- ``cv2`` is present in this
    fresh venv regardless of whether the split happened. It is still
    worth adding now: it becomes the real, load-bearing proof once
    ticket 004 lands and this venv genuinely has no ``cv2`` for any
    reason. The actual proof *right now* that the split accomplished
    anything is the ``sys.meta_path`` import-blocker test in
    ``tests/test_cv2_import_blocker_console_script.py``, which works
    independently of what this fresh venv happens to have installed."""
    venv_python = fresh_base_venv / "bin" / "python"
    result = _run([str(venv_python), "-c", "import aprilcam.client.renderer"])
    assert result.returncode == 0, (
        f"import aprilcam.client.renderer failed in a fresh base-only-install "
        f"venv (exit {result.returncode}):\nstdout={result.stdout}\nstderr={result.stderr}"
    )


@pytest.mark.integration
def test_import_aprilcam_cli_locate_succeeds_in_the_fresh_base_only_venv(fresh_base_venv: Path):
    """Bonus fix (sprint 036 ticket 005, not required by this ticket's
    own acceptance criteria): ``cli/locate.py`` imports
    ``calibration.geometry`` too (``solve_camera_geometry``), for the
    same reason ``client/renderer.py`` does -- see that test's own
    docstring above for why this passes trivially today and becomes
    load-bearing once ticket 004 lands."""
    venv_python = fresh_base_venv / "bin" / "python"
    result = _run([str(venv_python), "-c", "import aprilcam.cli.locate"])
    assert result.returncode == 0, (
        f"import aprilcam.cli.locate failed in a fresh base-only-install venv "
        f"(exit {result.returncode}):\nstdout={result.stdout}\nstderr={result.stderr}"
    )


# ---------------------------------------------------------------------------
# AC3 -- `import cv2` FAILS in the fresh venv. Currently does not: see
# the module docstring and design.md §9 item 1. xfail(strict=True) so
# this stays visible in the sprint gate without blocking it, and flips
# to a hard failure (not a silent pass) the day the gap closes.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.xfail(
    reason=(
        "known packaging gap, design.md §9 item 1: opencv-contrib-python "
        "is listed as a BASE dependency in pyproject.toml (not gated "
        "behind optional-dependencies.daemon), so a genuinely fresh, "
        "base-only-install venv still gets a working cv2. Confirmed by "
        "hand while writing this test: `uv pip install` of the built "
        "wheel with no extras pulls in opencv-contrib-python==5.0.0.93 "
        "unprompted. This must start passing -- and this xfail marker "
        "must be removed -- the day that dependency moves to the "
        "`daemon` extra."
    ),
    strict=True,
)
def test_import_cv2_fails_in_the_fresh_base_only_venv(fresh_base_venv: Path):
    venv_python = fresh_base_venv / "bin" / "python"
    result = _run([str(venv_python), "-c", "import cv2"])
    assert result.returncode != 0, (
        "`import cv2` unexpectedly SUCCEEDED in a fresh BASE-only install "
        "venv -- cv2 should only be reachable via the `daemon` extra:\n"
        f"stdout={result.stdout}"
    )


# ---------------------------------------------------------------------------
# AC4 -- the console script exists and reports the built wheel's version.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_console_script_exists_and_reports_the_built_wheel_version(
    fresh_base_venv: Path, built_wheel: Path
):
    script = fresh_base_venv / "bin" / "aprilcam"
    assert script.exists(), f"expected a console script at {script}"
    assert os.access(script, os.X_OK), f"{script} is not executable"

    # Wheel filename convention: aprilcam-<version>-<pytag>-<abitag>-<platform>.whl
    expected_version = built_wheel.name.split("-")[1]

    result = _run([str(script), "-V"])
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert result.stdout.strip() == f"aprilcam {expected_version}", (
        f"expected 'aprilcam {expected_version}', got {result.stdout!r} "
        f"(stderr={result.stderr!r})"
    )


# ---------------------------------------------------------------------------
# AC5 -- a consumer script, run from the fresh venv's own python,
# round-trips camera-free data against a private isolated daemon
# started from this (dev) venv.
# ---------------------------------------------------------------------------


@pytest.fixture
def short_runtime_dir() -> Iterator[Path]:
    d = _short_tmp_dir("ac-013-rt-")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def isolated_directories(tmp_path: Path, short_runtime_dir: Path, monkeypatch) -> Directories:
    """``config_dir``/``state_dir`` isolated via the flag tier
    (used directly by the in-process daemon side below);
    ``socket_dir`` isolated via ``XDG_RUNTIME_DIR`` (its only tier).
    Skips -- rather than silently talking to a real daemon -- if this
    sandbox's ``/run/aprilcam`` is itself available and writable,
    mirroring ``test_public_api_system.py``'s identically-named
    fixture and ``test_lifecycle.py``'s real-subprocess round trip."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_runtime_dir))
    directories = Directories(config_dir=tmp_path / "config", state_dir=tmp_path / "state")
    if directories.socket_dir != short_runtime_dir / "aprilcam":
        pytest.skip(
            "this sandbox's /run/aprilcam is available and writable, so "
            "socket_dir cannot be isolated down to a private tmp root -- "
            "cannot safely isolate this test from a real daemon here"
        )
    return directories


@pytest.fixture
def isolated_daemon(isolated_directories: Directories) -> Iterator[Directories]:
    """A private daemon, started **in-process from this (dev) venv**
    (which has ``cv2`` via ``[daemon]``) -- never a subprocess, and
    never the real system daemon. No camera is opened anywhere in this
    fixture (``daemon_main.build`` never opens a camera on its own),
    so it is camera-free by construction, matching
    ``test_public_api_system.py``'s ``daemon_only_session``."""
    lifecycle = daemon_main.build(isolated_directories)
    lifecycle.start()
    try:
        yield isolated_directories
    finally:
        lifecycle.stop()


@pytest.mark.integration
def test_consumer_script_in_fresh_venv_round_trips_camera_free_data_against_isolated_daemon(
    fresh_base_venv: Path,
    isolated_daemon: Directories,
    short_runtime_dir: Path,
    tmp_path: Path,
):
    """AC5: a consumer script, run via the **fresh base-only venv's own
    python** (not this test process's dev-venv python), connects to the
    private isolated daemon above and round-trips
    ``set_playfield``/``get_playfield``,
    ``put_annotation``/``get_annotations``/``clear_annotations``, and
    ``get_guide`` -- entirely camera-free."""
    venv_python = fresh_base_venv / "bin" / "python"
    script_path = tmp_path / "consumer.py"
    script_path.write_text(_CONSUMER_SCRIPT)

    env = dict(os.environ)
    env["APRILCAM_CONFIG_DIR"] = str(isolated_daemon.config_dir)
    env["APRILCAM_STATE_DIR"] = str(isolated_daemon.state_dir)
    env["XDG_RUNTIME_DIR"] = str(short_runtime_dir)

    result = _run([str(venv_python), str(script_path)], env=env)
    assert result.returncode == 0, (
        f"consumer script failed in the fresh base-only venv against the "
        f"isolated daemon (exit {result.returncode}):\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "CONSUMER_ROUND_TRIP_OK" in result.stdout, result.stdout
