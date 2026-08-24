"""The L1 flagship hardware test (sprint 035, ticket 011, UC-22/UC-12):
one uninterrupted real ``aprilcam mcp`` stdio session, against a private,
self-cleaning daemon and a real camera, driving golden path + drawing +
off-center-tag-height/parallax end to end -- entirely through the MCP
tool surface, the way an external AI agent actually would.

**Composes three already-proven patterns rather than inventing a
fourth.** Ticket 009's :mod:`tests.aprilcam.mcp.test_stdio_offline`
proved a real ``aprilcam mcp`` subprocess speaks real stdio JSON-RPC with
zero eager daemon RPCs; ticket 010's
:mod:`tests.aprilcam.calibration.test_full_calibrate_hardware` proved the
"private per-test daemon, gate on a real open/frame fetch, skip only
*this* parameterized case on failure" pattern generalizes across every
qualifying camera; :func:`tests.aprilcam.daemon.test_camera_geometry_hardware.
test_mount_z_is_a_noop_until_camera_position_is_pushed_then_engages_with_no_restart`
proved the mount_z no-op -> pushed-live -> engages-with-no-restart
mechanism against this exact rig's real, solved ``camera_position``. This
module is those three, composed: the private daemon is launched as a
real CLI subprocess (not ``daemon.__main__.build()`` in-process --
ticket 011's own plan mirrors ``test_camera_geometry_hardware.py:204-227``'s
``cli_env`` fixture exactly, because a *second*, independent ``aprilcam
mcp`` subprocess needs to resolve the *same* daemon through the ordinary
``Directories``/``XDG_RUNTIME_DIR`` socket-dir cascade -- a custom,
in-process-only ``socket_path`` (ticket 010's own ``daemon`` fixture)
would be unreachable from a genuinely separate process).

**Camera_position is sacred.** Every write in this module lands under
an isolated ``APRILCAM_CONFIG_DIR``/``APRILCAM_STATE_DIR``/
``XDG_RUNTIME_DIR`` root (a fresh ``tmp_path`` plus a short ``/tmp``
socket dir, never the real system roots) -- the
:func:`real_system_untouched_guard` fixture below spot-checks, with real
file fingerprints taken before and after, that the real
``~/.config/aprilcam``/``~/.local/share/aprilcam`` camera config and
calibration for the camera under test, plus the real system's
``main-playfield.json``, are byte-for-byte unchanged. The real
``camera_position`` this rig has actually solved
(``arducam-ov9782-usb-camera`` -> nadir ``(3.057, -2.799)`` cm, height
``128.230`` cm) is imported, never redeclared, from
``tests/aprilcam/daemon/mount_z_math.py`` -- the same shared helper
``test_camera_geometry_hardware.py`` uses, so the two modules cannot
silently drift apart (grep ``REAL_NADIR_X_CM = `` in this file: it does
not appear).

**You will almost certainly not be able to execute this test's happy
path in an agent session, and that is expected.** A private daemon
started from an agent process inherits Claude Code's macOS TCC
attribution, which has no camera grant -- enumeration
(``list_cameras``) keeps succeeding while every real open
(``get_frame``) fails with ``failed to open camera at index N``. See
``docs/knowledge/daemon-camera-grant-and-detection-priming.md`` for the
full mechanism. This module's own pre-flight gate
(:func:`_gate_or_skip`) turns that into a clean, per-parameterized-case
skip naming exactly what to do about it (run from an operator Terminal
session -- this sprint's ``test-hardware.sh``, ticket 035-017) rather
than a hard failure. Ticket 010 found 41 of 57 existing ``-m camera``
tests failing hard instead of skipping when the rig is unavailable --
this module is written to never join that count.

**Why the pre-flight gate does not pattern-match a specific error
string.** ``camera/backends/opencv_backend.py``'s ``failed to open
camera at index N`` is a bare ``RuntimeError``, not an
``aprilcam.errors.AprilCamError`` subtype -- so it is not, in general,
guaranteed to survive a tool-error boundary verbatim (``mcp/errors.py``'s
``handle_tool_errors`` only forwards ``str(exc)`` unchanged for
``AprilCamError`` subtypes; anything else becomes a generic ``"internal
error: {type name}"``, by design, to avoid leaking internals to an
agent). Confirmed empirically while writing this module: in practice the
text *does* currently survive the MCP round trip, because the daemon's
own gRPC layer maps an unrecognised server-side exception to an
``UNKNOWN`` status carrying ``str(exc)`` as its detail, and
``client/daemon_client.py``'s ``_reconstruct_error`` turns *that* back
into :class:`~aprilcam.errors.ConnectionFailed` (an ``AprilCamError``) --
one layer of accidental preservation this module does not want to
depend on, since neither the gRPC-detail contract nor the MCP boundary
promises to keep forwarding a message with no defined stability
guarantee. The gate below therefore still treats *any* ``isError`` on a
raw (non-deskewed -- never a ``NotCalibrated`` false-positive from an
as-yet-uncalibrated camera) ``get_frame`` as "this parameterized case
cannot run right now," quoting whatever text the server *did* return for
a human to read, rather than asserting on its exact contents.

**Rendering the drawn path is a genuine gap this module works around,
not routes around.** ``get_frame``'s MCP tool (``mcp/tools/
perception.py``) returns only encoded image bytes (``mcp/images.py``'s
``image_response``) -- never the ``ImageFrame`` dataclass's
``px_per_cm``/``world_extent``/``annotations`` fields, which live only
on the daemon-internal object ``client/renderer.py``'s ``Renderer``
needs to place a ``PathShape`` in pixel space. This is not a bug this
ticket works around by weakening the assertion: the project's own
documented contract is that a consumer needing pixel work "fetch[es] a
frame and process[es] it themselves" (``.claude/rules/project-overview.md``'s
"Frame Capture" section) -- exactly what this module does, in-process,
the same way ``aprilcam view`` does. ``calibration/deskew.py``'s
``Deskewer`` guarantees ``px_per_cm = output_width_px / field_width_cm``
*exactly*, on the width axis (its own docstring: "why width,
specifically, is the one dimension this scalar is guaranteed to satisfy
exactly") -- so decoding the fetched JPEG's own pixel width (Pillow) and
combining it with the playfield's own ``width_cm`` (``get_playfield``,
also an MCP tool) reconstructs the *exact* scale the daemon used, with
no daemon-internal value ever crossing the MCP boundary. This module
then builds its own local ``ImageFrame``/``Annotation`` from data it
already holds (the same points just sent to ``draw_path``) and calls
``client.renderer.Renderer.render()`` directly -- library code any MCP
consumer is free to use, not an MCP tool call -- to produce the
annotated PNG this test actually samples a pixel from.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from PIL import Image, ImageColor

from aprilcam.calibration.geometry import world_to_deskewed_pixel
from aprilcam.client.renderer import Renderer
from aprilcam.config.directories import Directories
from aprilcam.config.interfaces import CameraConfig, CameraPosition
from aprilcam.types import Annotation, ImageFrame, PathShape, Style, WorldPoint
from tests.aprilcam.daemon.conftest import hardware_test_camera_config
from tests.aprilcam.daemon.mount_z_math import (
    REAL_HEIGHT_CM,
    REAL_NADIR_X_CM,
    REAL_NADIR_Y_CM,
    expected_parallax_corrected_position,
)
from tests.aprilcam.hardware_camera import resolve_all_hardware_camera_identities

pytestmark = [pytest.mark.camera, pytest.mark.integration]

#: The real, checked-in playfield definition -- the same file every other
#: hardware/integration module names as ground truth. Loaded as a raw
#: dict, never a Python object: ``set_playfield`` is an MCP tool that
#: takes a JSON document (spec principle 7 -- no file paths cross the
#: MCP boundary).
MAIN_PLAYFIELD_JSON = (
    Path(__file__).resolve().parents[3] / "config" / "aprilcam-old" / "playfields" / "main-playfield.json"
)
MAIN_PLAYFIELD = "main-playfield"

#: This flow's own drawing layer/id -- namespaced to this ticket so a
#: concurrent run of some other suite's own annotations (there is no
#: real concurrency risk here, this daemon is private and torn down per
#: test, but the naming costs nothing and documents intent) can never be
#: confused with this one.
PATHS_LAYER = "ticket-035-011-paths"
PATH_ID = "ticket-035-011-path"

#: The off-center mount_z flow's target marker: ArUco **2** (surveyed
#: "north," world ``(0, 44.65)`` cm in ``main-playfield.json``), not
#: ArUco 1. ``test_camera_geometry_hardware.py`` uses ArUco 1 (~70cm
#: from this rig's real nadir) for its own single-camera mechanism
#: proof, but this module is parameterized across every qualifying
#: camera (``resolve_all_hardware_camera_identities``), which on this
#: project's own rig includes camera 5 (``arducam-ov9281-usb-camera``)
#: -- and this sprint's own live-measured rig facts are explicit that
#: ArUco 1 is only 7/12 reliable on camera 5 ("never require it"),
#: while ArUco 2 is 12/12 on both camera 3 and camera 5. ArUco 2 sits
#: ``hypot(0 - 3.057, 44.65 - (-2.799)) ~= 47.6`` cm from the real nadir
#: -- at ``mount_z=20`` that is ``(20 / 128.23) * 47.6 ~= 7.4`` cm of
#: expected displacement, comfortably clear of both the noise floor
#: (this module's own ``abs=1.0`` cm "unmoved" tolerance) and the
#: ``> 2.0`` cm displacement assertion below -- the same "clean,
#: reliably-above-noise signal" property ``test_camera_geometry_
#: hardware.py`` chose ArUco 1 for, just on a marker this module's own
#: two-camera parameterization can actually rely on every run.
MOUNT_Z_TEST_ARUCO_ID = 2

#: Detection needs priming (``docs/knowledge/daemon-camera-grant-and-
#: detection-priming.md``): the first poll after a camera opens returns
#: zero tags (``CONFIRMATION_GRACE=8``; measured poll 1 = 0, poll 2 =
#: 10, polls 3-5 = 11 on this rig). This many ``get_tags`` calls, spaced
#: out, comfortably clears that streak before this module trusts a tag
#: count or starts polling for one specific tag's world position.
_PRIME_ATTEMPTS = 10
_PRIME_DELAY_S = 0.3

#: How long :func:`_poll_for_world` retries before concluding a
#: specific tag genuinely is not visible right now (as opposed to still
#: within the confirmation streak) -- generous enough to absorb a few
#: missed frames without flaking, matching ``test_camera_geometry_
#: hardware.py``'s own ``_poll_for_tag`` attempts/delay.
_POLL_ATTEMPTS = 20
_POLL_DELAY_S = 0.5

#: The real system's own default :class:`Directories`, resolved **once,
#: at module import time** -- deliberately before this module's own
#: ``daemon_env`` fixture has a chance to ``monkeypatch.setenv``
#: ``APRILCAM_CONFIG_DIR``/``APRILCAM_STATE_DIR``/``XDG_RUNTIME_DIR`` for
#: its own private, ephemeral roots. Matches ``hardware_camera.py``'s own
#: ``_REAL_SYSTEM_DIRECTORIES`` precedent exactly, and for the identical
#: reason: constructing this lazily inside a fixture that runs *after*
#: ``daemon_env`` has already patched the environment would fingerprint
#: the *isolated* tmp roots, never the real ones -- silently vacuous.
_REAL_SYSTEM_DIRECTORIES = Directories()


def _short_tmp_dir(prefix: str) -> Path:
    """A short, top-level ``/tmp`` directory -- unix domain sockets have
    a ~103-character path limit on macOS/Linux, and pytest's
    ``tmp_path`` nests too deeply (matches every other real-subprocess
    hardware module's identically-named helper, e.g.
    ``test_camera_geometry_hardware.py``/``test_stdio_offline.py``)."""
    return Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))


def _aprilcam_bin() -> Path:
    """The real, installed ``aprilcam`` console script next to the
    interpreter running this test -- not ``python -m aprilcam`` or any
    other indirection, so this test spawns exactly what an operator or
    agent actually invokes. Skips cleanly if this environment has no
    such script (matches every other real-subprocess module)."""
    candidate = Path(sys.executable).parent / "aprilcam"
    if not candidate.exists():  # pragma: no cover - environment-dependent
        pytest.skip(f"no aprilcam console script next to {sys.executable}")
    return candidate


def _run(bin_path: Path, *args: str, env: Dict[str, str], timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run([str(bin_path), *args], capture_output=True, env=env, timeout=timeout)


def _fingerprint(path: Path) -> Optional[Tuple[int, str]]:
    """``(mtime_ns, sha256)`` for *path*, or ``None`` if it does not
    exist -- "absent" is itself a fingerprint state, so a test that
    somehow *creates* a real file where none existed before is caught
    too, not just a test that mutates one already there."""
    if not path.exists():
        return None
    return (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())


def _real_camera_paths(slug: str) -> Tuple[Path, Path, Path]:
    return (
        _REAL_SYSTEM_DIRECTORIES.config_dir / "cameras" / slug / "config.json",
        _REAL_SYSTEM_DIRECTORIES.state_dir / "cameras" / slug / "calibration.json",
        _REAL_SYSTEM_DIRECTORIES.config_dir / "playfields" / f"{MAIN_PLAYFIELD}.json",
    )


# ---------------------------------------------------------------------------
# Dynamic per-camera parameterization -- identical shape to ticket 010's
# test_full_calibrate_hardware.py: resolved once per collection (real
# hardware I/O, cached), one parameterized case per qualifying camera, a
# gap in one camera never removes another's case.
# ---------------------------------------------------------------------------

_resolved_identities_cache = None


def _resolved_identities():
    global _resolved_identities_cache
    if _resolved_identities_cache is None:
        _resolved_identities_cache = resolve_all_hardware_camera_identities(playfield=MAIN_PLAYFIELD)
    return _resolved_identities_cache


def pytest_generate_tests(metafunc):
    if "identity" in metafunc.fixturenames:
        identities = _resolved_identities()
        metafunc.parametrize("identity", identities, indirect=True, ids=[i.slug for i in identities])


@pytest.fixture
def identity(request):
    return request.param


@pytest.fixture
def daemon_env(tmp_path, monkeypatch) -> Iterable[Dict[str, str]]:
    """Isolated environment for a real ``aprilcam daemon`` **and** a real
    ``aprilcam mcp`` subprocess to share -- private ``config``/``state``
    roots and a private ``socket_dir``, so both processes resolve the
    *same* private daemon via the ordinary env-var cascade and this
    module can never reach the stakeholder's real ``~/.config/aprilcam``
    roots or his real, currently-running daemon's control socket.
    Matches ``test_camera_geometry_hardware.py:204-227``'s ``cli_env``
    fixture and ``test_stdio_offline.py``'s ``isolated_mcp_env`` exactly.
    """
    short_dir = _short_tmp_dir("ac-011-")
    try:
        monkeypatch.setenv("APRILCAM_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("APRILCAM_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_dir))

        directories = Directories()
        if directories.socket_dir != short_dir / "aprilcam":
            pytest.skip(
                "this sandbox's /run/aprilcam is available and writable, so "
                "socket_dir cannot be isolated down to a private tmp root -- "
                "cannot safely isolate a real daemon + real aprilcam mcp "
                "subprocess pair from the stakeholder's real, currently-"
                "running daemon here"
            )
        yield dict(os.environ)
    finally:
        shutil.rmtree(short_dir, ignore_errors=True)


@pytest.fixture
def real_system_untouched_guard(identity):
    """AC: "spot-checked that the real ``~/.config/aprilcam``/
    ``~/.local/share/aprilcam`` are unchanged before/after." Fingerprints
    (mtime + sha256, or "absent") the real system's own camera config,
    camera calibration, and ``main-playfield.json`` for *identity* --
    never this test's own isolated ``tmp_path`` roots -- before the test
    body runs, and asserts they are byte-for-byte identical afterward,
    in fixture teardown, so this check still runs even if the test body
    fails partway through.
    """
    paths = _real_camera_paths(identity.slug)
    before = tuple(_fingerprint(p) for p in paths)
    yield
    after = tuple(_fingerprint(p) for p in paths)
    assert after == before, (
        f"real system state changed during this isolated test run for "
        f"{identity.slug!r} -- this must never happen, this module only "
        f"ever writes under its own isolated tmp_path roots. paths={paths} "
        f"before={before} after={after}"
    )


# ---------------------------------------------------------------------------
# Small MCP-session helpers shared by the three flows below.
# ---------------------------------------------------------------------------


def _error_text(result) -> str:
    if result.content:
        return result.content[0].text
    return repr(result.content)


async def _call(session: ClientSession, tool: str, args: Dict[str, Any], *, timeout: float = 20.0):
    """Call *tool* and assert it succeeded -- every call in this module
    past the pre-flight gate is expected to succeed against a genuinely
    working rig; a failure here is a real bug, not a hardware-
    availability gap, so this asserts rather than skips."""
    with anyio.fail_after(timeout):
        result = await session.call_tool(tool, args)
    assert result.isError is False, f"{tool}({args}) failed: {_error_text(result)}"
    return result


async def _gate_or_skip(session: ClientSession, slug: str) -> None:
    """The real open/frame-fetch gate (``docs/knowledge/daemon-camera-
    grant-and-detection-priming.md``: "gate hardware tests on an open or
    a frame fetch succeeding, never on enumeration alone"). Skips this
    one parameterized case, cleanly, with remediation text, on either
    failure -- never a hard failure, and never a false pass from
    enumeration alone. See the module docstring's "pre-flight gate does
    not pattern-match" section for why this does not try to recognise
    the specific "failed to open camera" text.
    """
    cams = await session.call_tool("list_cameras", {})
    if cams.isError:
        pytest.skip(
            f"list_cameras itself failed over this isolated MCP session -- "
            f"{_error_text(cams)}"
        )
    names = {c["name"] for c in cams.structuredContent["result"]}
    if slug not in names:
        pytest.skip(
            f"{slug!r} not enumerated by this isolated daemon's own camera "
            f"registry right now (attached: {sorted(names)}) -- it qualified "
            f"against this machine's *real* system state (config+calibration "
            f"present against {MAIN_PLAYFIELD!r}) at collection time, but is "
            f"not visible to this fresh, private daemon instance."
        )

    # deskewed=False: this camera is not yet configured/calibrated in
    # this isolated daemon, so a deskewed request would raise the
    # (structurally different, AprilCamError-typed) NotCalibrated
    # instead of ever reaching the real open this gate cares about.
    gate = await session.call_tool(
        "get_frame", {"camera": slug, "deskewed": False, "format": "base64"}
    )
    if gate.isError:
        pytest.skip(
            f"{slug!r} qualifies on this machine's real system state but a "
            f"raw get_frame over this isolated MCP session failed: "
            f"{_error_text(gate)}. Expected when the process that launched "
            f"this private daemon has no camera TCC grant (an agent session "
            f"-- see docs/knowledge/daemon-camera-grant-and-detection-"
            f"priming.md) or the device is genuinely busy elsewhere; re-run "
            f"from an operator Terminal session (this sprint's "
            f"test-hardware.sh, ticket 035-017) to exercise this camera for "
            f"real."
        )


async def _prime(session: ClientSession, camera: str) -> None:
    for _ in range(_PRIME_ATTEMPTS):
        await _call(session, "get_tags", {"camera": camera})
        await anyio.sleep(_PRIME_DELAY_S)


async def _poll_for_world(
    session: ClientSession, camera: str, family: str, number: int
) -> Tuple[float, float]:
    last: Any = None
    for _ in range(_POLL_ATTEMPTS):
        result = await _call(session, "get_tags", {"camera": camera})
        observed = result.structuredContent.get("tags", [])
        for record in observed:
            tag = record["tag"]
            if tag["family"] == family and tag["number"] == number and record.get("world") is not None:
                return record["world"]["x"], record["world"]["y"]
        last = observed
        await anyio.sleep(_POLL_DELAY_S)
    pytest.skip(
        f"{family} {number} not detected with a world position across "
        f"{_POLL_ATTEMPTS} polls on camera {camera!r} -- physical field/"
        f"marker visibility may have changed mid-run. Last observed tags: "
        f"{last}"
    )
    raise AssertionError("unreachable")  # pragma: no cover - pytest.skip always raises


def _find_mount_entry(entries: List[Dict[str, Any]], family: str, number: int) -> Optional[Dict[str, Any]]:
    for entry in entries:
        tag = entry["tag"]
        if tag["family"] == family and tag["number"] == number:
            return entry
    return None


# ---------------------------------------------------------------------------
# The flagship test.
# ---------------------------------------------------------------------------



async def _run_flagship_session(session: ClientSession, identity) -> None:
    """The full session body: initialize, pre-flight gate, setup
    (playfield + camera config + calibrate), then golden path, paths,
    and off-center mount_z/parallax, in that order -- see
    :func:`test_mcp_stdio_hardware_flagship_session` for why this is a
    separate function.

    **Why this is not simply inlined in the test function.**
    ``stdio_client``/``ClientSession`` each run their own background
    read/write pump inside an ``anyio.create_task_group()``. Any
    exception raised directly inside their ``async with`` body --
    including a plain ``assert`` failure or a ``pytest.skip()`` call
    (``pytest.skip`` raises ``Skipped``, a ``BaseException``) -- gets
    caught by that task group's own ``__aexit__`` and re-raised as a
    ``BaseExceptionGroup("unhandled errors in a TaskGroup", ...)``,
    discarding this module's own carefully-written skip/failure message
    in favour of that generic anyio-internal one (confirmed directly:
    without this split, a skip inside the session surfaced to pytest as
    ``SKIPPED [...] anyio/_backends/_asyncio.py:783: unhandled errors in
    a TaskGroup`` -- with this function's own actionable text nowhere in
    the report, exactly what this ticket's "skip cleanly with
    remediation text" acceptance criterion forbids). The caller
    (:func:`test_mcp_stdio_hardware_flagship_session`) instead calls
    this function inside its own local ``try/except BaseException``,
    stores whatever was raised, lets both ``async with`` blocks exit
    normally (no exception in flight -> no task-group wrapping), and
    re-raises the *original* exception object once both are closed --
    outside any task group, so pytest sees the real ``Skipped``/
    ``AssertionError`` with its real message intact.
    """
    slug = identity.slug
    with anyio.fail_after(20):
        init_result = await session.initialize()
    assert init_result.serverInfo.name == "aprilcam", init_result.serverInfo

    # -- pre-flight gate: never assume the rig is usable from
    # enumeration alone (see module docstring + the knowledge
    # doc). Everything from here on is asserted to succeed.
    await _gate_or_skip(session, slug)

    # ---------------------------------------------------------
    # Setup: push playfield + camera config (no camera_position
    # -- "camera_position is sacred"), then calibrate. All
    # within this one session, all landing only in this
    # isolated daemon's own state.
    # ---------------------------------------------------------
    playfield_document = json.loads(MAIN_PLAYFIELD_JSON.read_text())
    await _call(session, "set_playfield", {"document": playfield_document})

    camera_config = hardware_test_camera_config(slug, playfield=MAIN_PLAYFIELD)
    assert camera_config.camera_position is None, (
        "hardware_test_camera_config must never carry a camera_position at "
        "this stage -- the off-center flow below pushes one deliberately, "
        "later, and only into this isolated daemon"
    )
    await _call(session, "set_camera_config", {"document": camera_config.to_dict()})

    calibration = await _call(session, "calibrate", {"camera": slug}, timeout=90.0)
    calibration_result = calibration.structuredContent
    assert calibration_result["succeeded"] is True, (
        f"calibration failed against the real field for {slug!r} -- "
        f"{calibration_result}. This module's own swept exposure "
        f"(hardware_test_camera_config) was already pushed above; if this is "
        f"a real regression rather than a transient occlusion, see "
        f"tests/aprilcam/daemon/test_calibration_hardware.py for the same "
        f"assertion against a single fixed camera."
    )

    await _prime(session, slug)

    # ---------------------------------------------------------
    # Flow 1: golden path -- get_guide -> list_cameras ->
    # get_tags -> where.
    # ---------------------------------------------------------
    guide = await _call(session, "get_guide", {})
    assert guide.content and "AprilCam" in guide.content[0].text

    cameras_after_setup = await _call(session, "list_cameras", {})
    names_after_setup = {c["name"] for c in cameras_after_setup.structuredContent["result"]}
    assert slug in names_after_setup

    tags_result = await _call(session, "get_tags", {"camera": slug})
    observed_tags = tags_result.structuredContent.get("tags", [])
    assert len(observed_tags) > 0, (
        f"no tags detected on {slug!r} after {_PRIME_ATTEMPTS} priming polls -- "
        f"real field visibility may be obstructed right now."
    )

    where_result = await _call(session, "where", {"playfield": MAIN_PLAYFIELD, "query": "orange"})
    where_matches = where_result.structuredContent["result"]
    assert len(where_matches) > 0, (
        "'orange' matched nothing against main-playfield's own surveyed dots -- "
        f"pushed definition may not be the expected one: {where_matches}"
    )

    # ---------------------------------------------------------
    # Flow 2: paths -- draw_path -> round trip + revision ->
    # deskewed get_frame -> rendered-pixel assertion at the
    # polyline midpoint -> clear_layer -> confirm gone.
    # ---------------------------------------------------------
    before_annotations = await _call(session, "list_annotations", {"playfield": MAIN_PLAYFIELD})
    revision_before = before_annotations.structuredContent["revision"]

    path_world_points = ((-30.0, 0.0), (30.0, 0.0))
    await _call(
        session,
        "draw_path",
        {
            "playfield": MAIN_PLAYFIELD,
            "layer": PATHS_LAYER,
            "id": PATH_ID,
            "points": [list(p) for p in path_world_points],
        },
    )

    listed = await _call(
        session, "list_annotations", {"playfield": MAIN_PLAYFIELD, "layer": PATHS_LAYER}
    )
    listed_annotations = listed.structuredContent["annotations"]
    assert listed.structuredContent["revision"] > revision_before, (
        "draw_path did not bump AnnotationSet.revision"
    )
    matching = [a for a in listed_annotations if a["id"] == PATH_ID]
    assert len(matching) == 1, f"expected exactly one round-tripped annotation: {listed_annotations}"
    round_tripped_points = matching[0]["shape"]["points"]
    assert round_tripped_points == [{"x": x, "y": y} for x, y in path_world_points], (
        f"draw_path did not round-trip its own points: {round_tripped_points}"
    )

    playfield_readback = await _call(session, "get_playfield", {"name": MAIN_PLAYFIELD})
    width_cm = playfield_readback.structuredContent["playfield"]["width_cm"]
    height_cm = playfield_readback.structuredContent["playfield"]["height_cm"]

    frame_result = await _call(
        session, "get_frame", {"camera": slug, "deskewed": True, "format": "base64"}
    )
    assert frame_result.content and frame_result.content[0].type == "image"
    raw_image_bytes = base64.b64decode(frame_result.content[0].data)
    decoded_frame = Image.open(io.BytesIO(raw_image_bytes)).convert("RGB")

    # px_per_cm is exact on the width axis by construction
    # (calibration/deskew.py's Deskewer docstring) -- this
    # reconstructs it purely from data this MCP session
    # already has, never a daemon-internal value (module
    # docstring's "Rendering the drawn path" section).
    px_per_cm = decoded_frame.width / width_cm
    reconstructed_frame = ImageFrame(
        data=raw_image_bytes,
        width=decoded_frame.width,
        height=decoded_frame.height,
        encoding="jpeg",
        timestamp=0.0,
        deskewed=True,
        px_per_cm=px_per_cm,
        world_extent=(width_cm, height_cm),
        tags=None,
        annotations=None,
    )
    drawn_path_annotation = Annotation(
        id=PATH_ID,
        layer=PATHS_LAYER,
        shape=PathShape(points=tuple(WorldPoint(x=x, y=y) for x, y in path_world_points)),
        style=Style(),
    )
    rendered_png = Renderer().render(reconstructed_frame, extra=(drawn_path_annotation,))
    rendered_image = Image.open(io.BytesIO(rendered_png)).convert("RGB")

    midpoint_world = WorldPoint(
        x=(path_world_points[0][0] + path_world_points[1][0]) / 2.0,
        y=(path_world_points[0][1] + path_world_points[1][1]) / 2.0,
    )
    midpoint_pixel = world_to_deskewed_pixel(
        midpoint_world, world_extent=(width_cm, height_cm), px_per_cm=px_per_cm
    )
    # Style() leaves color unset -> client/renderer.py's own
    # documented default ("yellow"), not re-imported from
    # that module's private constant.
    expected_rgb = ImageColor.getrgb("yellow")

    mx, my = round(midpoint_pixel.x), round(midpoint_pixel.y)
    sampled: List[Tuple[int, int, int]] = []
    found_match = False
    for dy in range(-3, 4):
        for dx in range(-3, 4):
            x, y = mx + dx, my + dy
            if 0 <= x < rendered_image.width and 0 <= y < rendered_image.height:
                pixel = rendered_image.getpixel((x, y))
                sampled.append(pixel)
                if all(abs(pixel[i] - expected_rgb[i]) <= 40 for i in range(3)):
                    found_match = True
        if found_match:
            break
    assert found_match, (
        f"no pixel within a 7x7 neighborhood of the polyline midpoint pixel "
        f"({mx}, {my}) matched the expected drawn color {expected_rgb} (default "
        f"annotation color 'yellow') -- sampled: {sampled[:12]}"
    )

    await _call(session, "clear_layer", {"playfield": MAIN_PLAYFIELD, "layer": PATHS_LAYER})
    after_clear = await _call(
        session, "list_annotations", {"playfield": MAIN_PLAYFIELD, "layer": PATHS_LAYER}
    )
    assert after_clear.structuredContent["annotations"] == []

    # ---------------------------------------------------------
    # Flow 3: off-center tag height / parallax -- register_tag
    # mount_z=20 on an off-nadir tag -> no-op (unconfigured
    # geometry) -> push real camera_position -> engages, with
    # no daemon restart -> unregister_tag -> restored.
    # ---------------------------------------------------------
    await _prime(session, slug)
    before_x, before_y = await _poll_for_world(session, slug, "aruco", MOUNT_Z_TEST_ARUCO_ID)

    registration = await _call(
        session,
        "register_tag",
        {"family": "aruco", "number": MOUNT_Z_TEST_ARUCO_ID, "mount_z": 20.0},
    )
    registration_warning = registration.structuredContent["result"]

    params_before = await _call(session, "list_tag_parameters", {})
    entry_before = _find_mount_entry(
        params_before.structuredContent["result"], "aruco", MOUNT_Z_TEST_ARUCO_ID
    )
    assert entry_before is not None, params_before.structuredContent
    assert entry_before["mount_z_applied"] is False, (
        f"mount_z_applied must be False before any camera geometry is pushed to "
        f"this isolated daemon -- entry={entry_before}, register_tag warning="
        f"{registration_warning!r}"
    )

    unconfigured_x, unconfigured_y = await _poll_for_world(
        session, slug, "aruco", MOUNT_Z_TEST_ARUCO_ID
    )
    assert unconfigured_x == pytest.approx(before_x, abs=1.0), (
        "mount_z registered on a camera with no camera_position moved the "
        f"reported position: before=({before_x}, {before_y}), unconfigured="
        f"({unconfigured_x}, {unconfigured_y}) -- expected the documented "
        "pre-029 no-op"
    )
    assert unconfigured_y == pytest.approx(before_y, abs=1.0)

    geometry_config = CameraConfig(
        name=camera_config.name,
        playfield=camera_config.playfield,
        settings=camera_config.settings,
        camera_position=CameraPosition(
            nadir=WorldPoint(x=REAL_NADIR_X_CM, y=REAL_NADIR_Y_CM),
            height_cm=REAL_HEIGHT_CM,
        ),
    )
    await _call(session, "set_camera_config", {"document": geometry_config.to_dict()})

    params_after = await _call(session, "list_tag_parameters", {})
    entry_after = _find_mount_entry(
        params_after.structuredContent["result"], "aruco", MOUNT_Z_TEST_ARUCO_ID
    )
    assert entry_after is not None, params_after.structuredContent
    assert entry_after["mount_z_applied"] is True, (
        f"mount_z_applied did not flip to True after pushing this rig's real, "
        f"solved camera_position mid-session (no restart): entry={entry_after}"
    )

    configured_x, configured_y = await _poll_for_world(session, slug, "aruco", MOUNT_Z_TEST_ARUCO_ID)
    expected_x, expected_y = expected_parallax_corrected_position(
        before_x, before_y, mount_z_cm=20.0
    )
    displacement_cm = math.hypot(configured_x - before_x, configured_y - before_y)
    assert displacement_cm > 2.0, (
        "pushing camera_position over MCP did not measurably move the mount_z-"
        f"corrected position on the next detection burst: before=({before_x}, "
        f"{before_y}), configured=({configured_x}, {configured_y})"
    )
    assert configured_x == pytest.approx(expected_x, abs=2.0), (
        f"mount_z-corrected world.x {configured_x} not within tolerance of "
        f"expected {expected_x:.2f} (raw before={before_x})"
    )
    assert configured_y == pytest.approx(expected_y, abs=2.0)

    await _call(session, "unregister_tag", {"family": "aruco", "number": MOUNT_Z_TEST_ARUCO_ID})
    restored_x, restored_y = await _poll_for_world(session, slug, "aruco", MOUNT_Z_TEST_ARUCO_ID)
    assert restored_x == pytest.approx(before_x, abs=1.0), (
        f"position did not return to its pre-registration value after "
        f"unregister_tag: before=({before_x}, {before_y}), restored=("
        f"{restored_x}, {restored_y})"
    )
    assert restored_y == pytest.approx(before_y, abs=1.0)

    print(
        f"\n[035-011 hardware] camera={slug} tag=aruco {MOUNT_Z_TEST_ARUCO_ID} "
        f"raw={(before_x, before_y)} unconfigured={(unconfigured_x, unconfigured_y)} "
        f"configured={(configured_x, configured_y)} displacement_cm={displacement_cm:.2f} "
        f"restored={(restored_x, restored_y)}"
    )


async def test_mcp_stdio_hardware_flagship_session(identity, daemon_env, real_system_untouched_guard):
    """One real ``aprilcam mcp`` stdio session, against a private daemon
    and *identity*'s real camera: push playfield + camera config (never
    ``camera_position``) and calibrate, then -- without ever
    disconnecting -- golden path, drawing (paths), and off-center
    mount_z/parallax, in that order. See the module docstring for what
    each flow proves and for the documented gap (this session's own
    process has no camera TCC grant, so the gate below is expected to
    skip every parameterized case run this way). The real work lives in
    :func:`_run_flagship_session`; see that function's own docstring for
    why this wrapper exists at all (shielding pytest.skip/assert from
    anyio's TaskGroup exception wrapping).
    """
    env = daemon_env
    bin_path = _aprilcam_bin()

    start = _run(bin_path, "daemon", "start", env=env)
    assert start.returncode == 0, start.stderr
    try:
        outcome: Dict[str, BaseException] = {}
        params = StdioServerParameters(command=str(bin_path), args=["mcp"], env=env)
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(
                read_stream, write_stream, read_timeout_seconds=timedelta(seconds=30)
            ) as session:
                try:
                    await _run_flagship_session(session, identity)
                except BaseException as exc:  # noqa: BLE001 - see _run_flagship_session's docstring
                    outcome["exception"] = exc
        if "exception" in outcome:
            raise outcome["exception"]
    finally:
        stop = _run(bin_path, "daemon", "stop", env=env)
        assert stop.returncode == 0, stop.stderr
