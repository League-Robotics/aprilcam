"""End-to-end integration and acceptance for the whole ``mcp/`` subsystem
(ticket 026-006 -- the sprint's acceptance gate and the last ticket in
serial execution order; ``completes_issue: true``).

Tickets 001-005 each unit-tested their own tool group against a fake
``Daemon``, driven through ``FastMCP``'s low-level ``CallToolRequest``
handler (``test_server.py``'s ``_call_tool`` pattern, reused by every
``test_*.py`` module in this package). That proves tool *logic*.  This
module's job is **composition**: prove the tools work together in one
real MCP client session, and that the sprint's cross-cutting properties
(late binding against a *real* daemon restart, no-``cv2`` across the
whole package, mypy across the whole package) hold at the package level,
not per-module.

**A real ``ClientSession``, not the low-level handler.** Every test here
drives the server through ``mcp.shared.memory.
create_connected_server_and_client_session`` -- the ``mcp`` SDK's
in-memory client/server transport (a real ``ClientSession`` wired to a
real ``FastMCP`` instance via paired ``anyio`` memory streams, no
subprocess, no socket). This is a genuine MCP protocol round trip
(initialize handshake, ``tools/call`` requests, structured results) the
same way a real agent would drive this server over stdio -- a step up
from tickets 001-005's own low-level-handler tests, which is exactly
what this ticket's Testing section asks for: "an in-process MCP client
session ... drives the full golden path."

**Four scenarios, three markers:**

* :func:`test_golden_path_against_a_fake_daemon` -- no marker, fully
  headless: the issue's Verification section's golden path
  (``get_guide`` -> ``list_cameras`` -> ``set_playfield`` ->
  ``set_camera_config`` -> ``calibrate`` -> ``get_tags`` -> ``get_frame``
  -> ``draw_marker`` -> ``list_annotations`` -> ``clear_layer``) against
  an in-memory fake ``Daemon``.
* :func:`test_no_daemon_at_startup_then_recovers_once_one_starts` and
  :func:`test_daemon_restart_mid_session_fails_then_recovers` --
  ``@pytest.mark.integration``: UC-22.S-2/UC-22.S-3 against a **real**
  daemon process (``daemon.__main__.build``, matching
  ``tests/aprilcam/client/test_lifecycle.py``'s own "no camera hardware
  needed" integration-marked precedent -- a daemon with zero cameras is
  a valid, complete start, UC-01.S-2). These are what prove ticket 001's
  connection-manager cache-and-evict-on-failure path against a genuine
  restart, not only the fake ``Discovery`` ticket 001's own unit tests
  used.
* :func:`test_camera_golden_path_against_real_daemon_and_camera_3` --
  ``@pytest.mark.camera``: the same golden path as the fake-daemon test,
  against a real daemon and whichever attached camera is genuinely
  configured and calibrated against the physical ``main-playfield``
  (discovered at run time via
  ``tests/aprilcam/hardware_camera.py``, never a hardcoded device name --
  camera hardware varies by machine, and a specific device can also be
  genuinely busy in another concurrent process) -- matches the issue's
  Verification section exactly, skips cleanly if no such camera is
  attached (every ``@pytest.mark.camera`` module's convention).

**A real bug this ticket's own no-cv2 sweep found and fixed.** Writing
:func:`test_cv2_absent_from_sys_modules_after_importing_every_mcp_module`
as a genuine *dynamic* sweep (import every ``mcp/`` submodule in a clean
subprocess, then check ``sys.modules`` -- not the AST-only literal-import
scan ``tests/test_no_cv2_in_client_modules.py`` already does, which
cannot see a transitive import) surfaced that ``client/daemon_client.py``
and ``client/models.py`` both imported the concrete, ``cv2``-importing
``aprilcam.calibration.calibration.Calibration`` (for ``get_calibration``'s
return type and construction) -- so importing *any* ``mcp/`` module
(``mcp/connection.py`` -> ``client/discovery.py`` -> ``client/
daemon_client.py``) pulled ``cv2`` into ``sys.modules``, even though no
``mcp/`` file itself ever wrote ``import cv2``. This broke
``.claude/rules/project-overview.md``'s stated contract ("the MCP server
... require[s] only Pillow, NumPy, mss, and python-dotenv" -- ``cv2`` is
a daemon-only dependency) and would have crashed ``aprilcam mcp`` on
first tool call on any install without the ``[daemon]`` extra. Fixed at
the root: both modules now import and construct
``calibration.interfaces.Calibration`` (the abstract, ``cv2``-free base
already declared by ``daemon.interfaces.Daemon.get_calibration``'s own
signature) instead of the concrete cv2-backed subclass, and
``client/models.py``'s ``calibration_from_proto`` no longer carries the
``homography``/``matched_ids``/``reprojection_error_cm`` fields across
the wire boundary at all -- nothing on the client side of the wire ever
read them (grepped ``client/``, ``cli/``, ``mcp/``, ``view/`` to
confirm), and ``mcp/tools/configuration.py``'s own docstring already
documented the intent that ``get_calibration`` reports exactly
``stale``/``calibrated_at``/``playfield``, "not the internal homography
matrix." See those two modules' diffs and docstrings for the full fix.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from aprilcam.calibration.interfaces import CalibrationResult
from aprilcam.client.discovery import Discovery
from aprilcam.config.directories import Directories
from aprilcam.config.interfaces import CameraConfig
from aprilcam.daemon import __main__ as daemon_main
from aprilcam.mcp.connection import ConnectionManager
from aprilcam.mcp.server import build_server
from aprilcam.types import (
    Annotation,
    AnnotationSet,
    CameraState,
    ImageFrame,
    TagFrame,
)
from tests.aprilcam.hardware_camera import resolve_hardware_camera_slug

#: An arbitrary, self-describing camera name for Part 1's fake-daemon
#: golden path below -- ``_GoldenFakeDaemon`` never touches real hardware,
#: so unlike this module's real ``@pytest.mark.camera`` test further down,
#: there is nothing here to discover: any string works equally well as the
#: fake's camera name. Deliberately not a real device slug (this used to
#: be the same hardcoded ``ARDUCAM_SLUG`` the real-hardware test also
#: used, which blurred "arbitrary fake label" with "the real, discovered
#: camera" -- the two are unrelated and are now named accordingly).
FAKE_CAMERA_SLUG = "fake-golden-path-camera"

#: The real, checked-in playfield definition -- the same file every
#: other hardware module names as ground truth. Loaded as a raw dict
#: (not ``main_playfield_definition()``'s ``PlayfieldDefinition``
#: object) because ``set_playfield`` is an MCP tool that takes a JSON
#: document, never a Python object or a file path (spec principle 7).
MAIN_PLAYFIELD_JSON = (
    Path(__file__).resolve().parents[3] / "config" / "aprilcam-old" / "playfields" / "main-playfield.json"
)


def _main_playfield_document() -> Dict[str, Any]:
    return json.loads(MAIN_PLAYFIELD_JSON.read_text())


def _resolve_real_camera_slug() -> str:
    """The slug of a currently-attached camera that is genuinely
    configured and calibrated against ``main-playfield`` on this
    machine's real system state -- see
    ``tests/aprilcam/hardware_camera.py`` for the discovery contract.
    Skips cleanly (rather than hardcoding a device name) if none
    qualifies."""
    return resolve_hardware_camera_slug()


# ---------------------------------------------------------------------------
# Part 1: the golden path, in-process, against a fake daemon (headless)
# ---------------------------------------------------------------------------


@dataclass
class _GoldenFakeDaemon:
    """A single fake covering every daemon method the golden path
    touches (list_cameras/set_playfield/set_camera_config/calibrate/
    get_tags/get_frame/put_annotation/get_annotations/
    clear_annotations) -- the composed union of ticket 002/003/005's own
    per-module fakes (``test_perception.py``/``test_configuration.py``/
    ``test_annotations.py``), because this test is exactly what those
    individual fakes were never meant to prove: one session, every tool
    group, in order. The annotation half is a genuine tiny in-memory
    store (``test_annotations.py``'s own pattern), not just a call
    counter, so ``list_annotations`` after ``draw_marker`` and after
    ``clear_layer`` exercise real upsert/clear semantics.
    """

    cameras: Tuple[CameraState, ...] = ()
    calibration_result: Optional[CalibrationResult] = None
    tag_frame: Optional[TagFrame] = None
    frame: Optional[ImageFrame] = None

    pushed_playfield: Any = None
    pushed_camera_config: Optional[CameraConfig] = None
    requested_camera: Optional[str] = None

    _store: Dict[str, Dict[str, Dict[str, Annotation]]] = field(default_factory=dict)
    _revision: int = 0

    def list_cameras(self) -> Tuple[CameraState, ...]:
        return self.cameras

    def set_playfield(self, definition: Any) -> None:
        self.pushed_playfield = definition

    def set_camera_config(self, config: CameraConfig) -> None:
        self.pushed_camera_config = config

    def calibrate(self, camera: str, *, estimate_intrinsics: bool = False) -> CalibrationResult:
        self.requested_camera = camera
        assert self.calibration_result is not None, "test forgot to set calibration_result"
        return self.calibration_result

    def get_tags(self, camera: str) -> TagFrame:
        self.requested_camera = camera
        return self.tag_frame if self.tag_frame is not None else TagFrame(tags=(), timestamp=0.0, frame_index=0)

    def get_frame(self, camera: str, *, deskewed: bool = False, encoding: str = "jpeg") -> ImageFrame:
        self.requested_camera = camera
        return self.frame if self.frame is not None else ImageFrame(
            data=b"\xff\xd8\xff\xd9", width=4, height=4, encoding=encoding, timestamp=0.0
        )

    def put_annotation(self, playfield: str, annotation: Annotation) -> None:
        self._revision += 1
        self._store.setdefault(playfield, {}).setdefault(annotation.layer, {})[annotation.id] = annotation

    def clear_annotations(self, playfield: str, layer: Optional[str] = None) -> None:
        self._revision += 1
        if layer is None:
            self._store.pop(playfield, None)
        else:
            self._store.get(playfield, {}).pop(layer, None)

    def get_annotations(self, playfield: str, layer: Optional[str] = None) -> AnnotationSet:
        layers = self._store.get(playfield, {})
        if layer is not None:
            entries: Tuple[Annotation, ...] = tuple(layers.get(layer, {}).values())
        else:
            entries = tuple(a for layer_map in layers.values() for a in layer_map.values())
        return AnnotationSet(annotations=entries, revision=self._revision, timestamp=123.0)


@dataclass
class _FakeDiscovery:
    daemon: Any = None
    connect_calls: List[Optional[str]] = field(default_factory=list)

    def connect(self, host: Optional[str] = None) -> Any:
        self.connect_calls.append(host)
        return self.daemon

    def find_all(self):  # pragma: no cover - this session never fans out
        return ()


def _fake_connection_factory(daemon: Any):
    discovery = _FakeDiscovery(daemon=daemon)

    def _factory(*, default_host: Optional[str] = None) -> ConnectionManager:
        return ConnectionManager(default_host=default_host, discovery_factory=lambda: discovery)

    return _factory


async def test_golden_path_against_a_fake_daemon(tmp_path):
    """The issue's Verification section, driven end to end through a
    real ``ClientSession``, no shell access, against an in-memory fake
    ``Daemon``: orient via the guide, list cameras, push a playfield and
    camera config, calibrate, read tags, fetch a deskewed frame, draw an
    annotation and read it back, then clean up."""
    daemon = _GoldenFakeDaemon(
        cameras=(
            CameraState(
                name=FAKE_CAMERA_SLUG, present=True, usable=True, playfield=None, calibrated=False, number=3
            ),
        ),
        calibration_result=CalibrationResult(
            succeeded=True,
            matched_ids=tuple(range(1, 11)),
            missing_ids=(),
            reprojection_error_cm=0.18,
            lens_model_source="none",
        ),
    )
    server = build_server(
        connection_factory=_fake_connection_factory(daemon),
        temp_dir_factory=lambda: tmp_path,
    )

    async with create_connected_server_and_client_session(server) as session:
        # -- orient: works before this session has done anything else --
        guide = await session.call_tool("get_guide", {})
        assert guide.isError is False
        assert "AprilCam" in guide.content[0].text

        # -- perceive: what cameras exist --
        cameras = await session.call_tool("list_cameras", {})
        assert cameras.isError is False
        assert cameras.structuredContent is not None
        names = {c["name"] for c in cameras.structuredContent["result"]}
        assert FAKE_CAMERA_SLUG in names

        # -- configure: push a playfield, then link the camera to it --
        set_pf = await session.call_tool("set_playfield", {"document": _main_playfield_document()})
        assert set_pf.isError is False, set_pf.content
        assert daemon.pushed_playfield is not None
        assert daemon.pushed_playfield.name == "main-playfield"

        set_cc = await session.call_tool(
            "set_camera_config",
            {"document": {"name": FAKE_CAMERA_SLUG, "playfield": "main-playfield", "settings": {}}},
        )
        assert set_cc.isError is False, set_cc.content
        assert daemon.pushed_camera_config is not None
        assert daemon.pushed_camera_config.name == FAKE_CAMERA_SLUG

        # -- calibrate --
        cal = await session.call_tool("calibrate", {"camera": FAKE_CAMERA_SLUG})
        assert cal.isError is False, cal.content
        assert cal.structuredContent == {
            "succeeded": True,
            "matched_ids": list(range(1, 11)),
            "missing_ids": [],
            "reprojection_error_cm": 0.18,
            "reason": None,
            # sprint 030 ticket 002: absent this call (no
            # estimate_intrinsics argument passed above -> defaults to
            # False -> the concept never ran).
            "intrinsics_estimated": None,
            "intrinsics_mode": None,
            "elevated_features_used": None,
            # sprint 044: which lens model the call applied.
            "lens_model_source": "none",
            "lens_warning": None,
        }

        # -- perceive again: tags, then a deskewed frame --
        tags = await session.call_tool("get_tags", {"camera": FAKE_CAMERA_SLUG})
        assert tags.isError is False

        frame = await session.call_tool(
            "get_frame", {"camera": FAKE_CAMERA_SLUG, "deskewed": True, "format": "base64"}
        )
        assert frame.isError is False, frame.content
        assert len(frame.content) == 1
        assert frame.content[0].type == "image"
        assert base64.b64decode(frame.content[0].data) == b"\xff\xd8\xff\xd9"

        # -- annotate: draw, read back, clean up --
        draw = await session.call_tool(
            "draw_marker",
            {
                "playfield": "main-playfield",
                "layer": "acceptance",
                "id": "agent-marker",
                "x": 10.0,
                "y": -5.0,
                "label": "ticket 026-006",
            },
        )
        assert draw.isError is False, draw.content

        listed = await session.call_tool(
            "list_annotations", {"playfield": "main-playfield", "layer": "acceptance"}
        )
        assert listed.isError is False
        assert listed.structuredContent is not None
        ids = {a["id"] for a in listed.structuredContent["annotations"]}
        assert "agent-marker" in ids, f"the marker just drawn was not found by list_annotations: {ids}"

        cleared = await session.call_tool(
            "clear_layer", {"playfield": "main-playfield", "layer": "acceptance"}
        )
        assert cleared.isError is False, cleared.content

        after_clear = await session.call_tool(
            "list_annotations", {"playfield": "main-playfield", "layer": "acceptance"}
        )
        assert after_clear.structuredContent["annotations"] == []


# ---------------------------------------------------------------------------
# Part 2: real-daemon fixtures shared by the @integration and @camera tests
# below -- isolated lock/socket/config/state directories, matching
# tests/aprilcam/daemon/test_calibration_hardware.py's cli_env fixture,
# adapted for an in-process (no subprocess) real daemon + real Discovery.
# ---------------------------------------------------------------------------


@pytest.fixture
def real_directories(tmp_path, monkeypatch):
    """A :class:`~aprilcam.config.directories.Directories` pointed
    entirely at private, per-test paths -- ``config_dir``/``state_dir``
    under ``tmp_path`` directly, ``socket_dir`` via ``XDG_RUNTIME_DIR``
    (the only lever ``Directories`` exposes for it -- "sockets are not
    configurable at any tier" per that module's own docstring). Skips
    (rather than risk colliding with a real running daemon) if this
    sandbox's ``/run/aprilcam`` happens to be writable, exactly
    mirroring every other ``@pytest.mark.camera``/``integration`` module's
    identically-reasoned skip.
    """
    short_dir = Path(tempfile.mkdtemp(prefix="ac-026-006-", dir="/tmp"))
    try:
        monkeypatch.setenv("APRILCAM_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("APRILCAM_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_dir))
        directories = Directories()
        if directories.socket_dir != short_dir / "aprilcam":
            pytest.skip(
                "this sandbox's /run/aprilcam is available and writable, so "
                "socket_dir cannot be isolated down to a private tmp root -- "
                "cannot safely isolate a real-daemon test from a real daemon here"
            )
        yield directories
    finally:
        shutil.rmtree(short_dir, ignore_errors=True)


def _real_connection_factory(directories: Directories, *, connect_timeout: float = 2.0):
    """Points ``mcp/connection.py``'s ``ConnectionManager`` at a real
    local daemon via a real ``client.discovery.Discovery`` bound to
    *directories* -- the same object identity the real daemon under test
    was built against, so both sides agree on the lock/socket path with
    no separate override plumbing needed. A short *connect_timeout*
    (versus ``Discovery``'s 5s default) keeps the "no daemon yet"
    half of these tests fast rather than needlessly slow."""

    def _factory(*, default_host: Optional[str] = None) -> ConnectionManager:
        return ConnectionManager(
            default_host=default_host,
            discovery_factory=lambda: Discovery(directories=directories, connect_timeout=connect_timeout),
        )

    return _factory


def _build_server_for(directories: Directories, tmp_path: Path):
    return build_server(
        connection_factory=_real_connection_factory(directories),
        temp_dir_factory=lambda: tmp_path,
    )


# ---------------------------------------------------------------------------
# Part 3: UC-22.S-2/UC-22.S-3 against a genuine daemon process -- no camera
# hardware required (a daemon with zero cameras is a valid, complete
# start, UC-01.S-2), so these run in the standard headless gate
# (`-m "not camera"` still includes `integration`).
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_no_daemon_at_startup_then_recovers_once_one_starts(real_directories, tmp_path):
    """SUC-005 / UC-22.S-2, for real: the server starts and answers
    ``get_guide`` with **no daemon running anywhere**; a daemon-backed
    tool call fails with a clean tool-level error (not a crashed
    session); starting a real daemon and retrying the identical call in
    the *same* session succeeds."""
    directories = real_directories
    server = _build_server_for(directories, tmp_path)

    async with create_connected_server_and_client_session(server) as session:
        guide = await session.call_tool("get_guide", {})
        assert guide.isError is False, "get_guide must succeed with zero daemon connection attempts"

        before = await session.call_tool("list_cameras", {})
        assert before.isError is True, "a daemon-backed call with no daemon running must fail cleanly"
        assert "Traceback" not in before.content[0].text

        # The session itself must still be perfectly usable -- a failed
        # tool call is not a crashed transport.
        still_alive = await session.call_tool("get_guide", {})
        assert still_alive.isError is False

        lifecycle = daemon_main.build(directories)
        lifecycle.start()
        try:
            after = await session.call_tool("list_cameras", {})
            assert after.isError is False, after.content
            assert isinstance(after.structuredContent["result"], list)
        finally:
            lifecycle.stop()


@pytest.mark.integration
async def test_daemon_restart_mid_session_fails_then_recovers(real_directories, tmp_path):
    """SUC-005 / UC-22.S-3, against a **real** daemon process (not the
    fake ``Discovery`` ticket 001's own unit tests scripted): a
    successful call caches a connection; the daemon is stopped and
    restarted underneath the same MCP session; the in-flight-equivalent
    next call fails cleanly; the call *after that* reconnects and
    succeeds -- ticket 001's cache-and-evict-on-failure path, proven for
    real."""
    directories = real_directories
    lifecycle = daemon_main.build(directories)
    lifecycle.start()
    try:
        server = _build_server_for(directories, tmp_path)

        async with create_connected_server_and_client_session(server) as session:
            first = await session.call_tool("list_cameras", {})
            assert first.isError is False, first.content

            # -- restart the real daemon process underneath the session --
            lifecycle.stop()

            with anyio.fail_after(30):
                during_restart = await session.call_tool("list_cameras", {})
            assert during_restart.isError is True, (
                "a call against a daemon that just stopped must fail cleanly, "
                "not hang and not crash the session"
            )
            assert "Traceback" not in during_restart.content[0].text

            lifecycle.start()

            with anyio.fail_after(30):
                recovered = await session.call_tool("list_cameras", {})
            assert recovered.isError is False, (
                f"the call after the daemon came back must reconnect and succeed: {recovered.content}"
            )
            assert isinstance(recovered.structuredContent["result"], list)
    finally:
        lifecycle.stop()


# ---------------------------------------------------------------------------
# Part 4: the @camera-marked golden path -- the issue's Verification
# section against real hardware.
# ---------------------------------------------------------------------------


#: The real system's own default :class:`Directories`, resolved **once,
#: at module import time** -- deliberately before any test's
#: ``real_directories`` fixture has a chance to ``monkeypatch.setenv``
#: ``APRILCAM_CONFIG_DIR``/``APRILCAM_STATE_DIR``/``XDG_RUNTIME_DIR``
#: for its own private, ephemeral directories. Constructing this lazily
#: instead (inside :func:`_find_real_prior_calibration`, called from
#: within a test that already depends on the patched
#: ``real_directories`` fixture) would resolve the *ephemeral* state
#: dir, not the real one, since ``monkeypatch.setenv`` changes
#: ``os.environ`` process-wide for the life of the test -- exactly the
#: bug this module-level constant sidesteps by resolving before that
#: patching ever happens.
_REAL_SYSTEM_DIRECTORIES = Directories()


def _find_real_prior_calibration(slug: str) -> Optional[Path]:
    """The *real system's* own ``calibration.json`` for *slug*, if this
    machine has ever calibrated it before (a normal daemon run, or an
    earlier hardware test/sprint's own manual verification pass) --
    read-only, via :data:`_REAL_SYSTEM_DIRECTORIES`, never written to.
    Used to boot-load a known-good, genuinely-real calibration into this
    test's own *ephemeral* ``state_dir`` (never the real one) so the
    golden path's "calibrate (or confirm already calibrated)" step
    means something even at a moment the physical field happens to be
    partially obstructed -- see this test's docstring. ``None`` if this
    machine has never calibrated this camera; the test still runs,
    relying on the live ``calibrate`` call alone in that case.
    """
    candidate = _REAL_SYSTEM_DIRECTORIES.state_dir / "cameras" / slug / "calibration.json"
    return candidate if candidate.exists() else None


@pytest.mark.camera
async def test_camera_golden_path_against_real_daemon_and_camera_3(real_directories, tmp_path):
    """The same session as :func:`test_golden_path_against_a_fake_daemon`,
    against a real daemon and camera 3 (``arducam-ov9782-usb-camera``)
    aimed at the physical ``main-playfield`` -- "orient via the guide,
    list cameras, calibrate, read tags in world coordinates, fetch a
    deskewed frame, draw a marker, and read it back," the issue's
    Verification section.

    **"calibrate (or confirm already calibrated)."** A fresh live
    ``calibrate`` call needs the *entire* physical field unobstructed at
    the exact moment this test runs -- a condition this module cannot
    control or verify from software (see
    :func:`_find_real_prior_calibration`'s docstring and this ticket's
    own completion notes for a real, disclosed instance: during this
    ticket's own hardware pass, an object had been left draped across
    part of the physical field, occluding one ArUco marker -- confirmed
    by fetching and visually inspecting a real raw frame, and reproduced
    identically by the pre-existing, untouched ``tests/aprilcam/daemon/
    test_calibration_hardware.py::
    test_full_set_calibration_matches_all_ten_required_aruco_ids``, so
    it is an environmental condition, not a regression from this
    ticket). Rather than hard-requiring a perfectly clear field for this
    one test run to mean anything, this test boot-loads this *machine's
    own real, previously-solved* calibration (if one exists) into its
    private, ephemeral ``state_dir`` -- genuinely real data, not
    fabricated -- so ``get_calibration`` can *confirm* an existing good
    calibration regardless of the field's exact state right now, while a
    live ``calibrate`` call is still made and its real result (success
    or an honestly-reported failure) is asserted to leave that prior
    good calibration untouched (UC-04's own contract), never silently
    corrupting it. If this machine has never calibrated this camera
    before, this falls back to requiring the live call to succeed.
    """
    arducam_slug = _resolve_real_camera_slug()
    directories = real_directories

    # -- boot-load the playfield + camera-config link, and (if this
    # machine has one on record) a real prior calibration -- see the
    # docstring above and _find_real_prior_calibration's own docstring.
    (directories.config_dir / "playfields").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(MAIN_PLAYFIELD_JSON, directories.config_dir / "playfields" / "main-playfield.json")
    (directories.config_dir / "cameras" / arducam_slug).mkdir(parents=True, exist_ok=True)
    (directories.config_dir / "cameras" / arducam_slug / "config.json").write_text(
        json.dumps({"name": arducam_slug, "playfield": "main-playfield", "settings": {}}), encoding="utf-8"
    )
    prior_calibration = _find_real_prior_calibration(arducam_slug)
    if prior_calibration is not None:
        cal_dir = directories.state_dir / "cameras" / arducam_slug
        cal_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(prior_calibration, cal_dir / "calibration.json")

    lifecycle = daemon_main.build(directories)
    lifecycle.start()
    try:
        server = _build_server_for(directories, tmp_path)

        # See _run_camera_golden_path_session's own docstring for why the
        # session body is a separate function called inside its own
        # try/except BaseException here, rather than inlined directly in
        # this async-with body: a pytest.skip()/assert raised straight
        # inside stdio_client/ClientSession's own anyio task group gets
        # swallowed into an opaque "unhandled errors in a TaskGroup" with
        # no message (ticket 035-011's finding, applied here).
        outcome: Dict[str, BaseException] = {}
        async with create_connected_server_and_client_session(server) as session:
            try:
                await _run_camera_golden_path_session(session, arducam_slug)
            except BaseException as exc:  # noqa: BLE001 - see docstring above
                outcome["exception"] = exc
        if "exception" in outcome:
            raise outcome["exception"]
    finally:
        lifecycle.stop()


async def _run_camera_golden_path_session(session, arducam_slug: str) -> None:
    """The full session body for :func:`test_camera_golden_path_against_
    real_daemon_and_camera_3` -- extracted into its own function so the
    caller can run it inside a local ``try/except BaseException``,
    outside ``create_connected_server_and_client_session``'s own
    ``async with`` block, before re-raising. ``create_connected_server_
    and_client_session`` (the in-memory client/server transport) runs its
    own background read/write pump inside an ``anyio.create_task_group()``
    -- like ``mcp.client.stdio.stdio_client``, any exception raised
    directly inside its ``async with`` body, including a plain ``assert``
    failure or a ``pytest.skip()`` call (``pytest.skip`` raises
    ``Skipped``, a ``BaseException``), gets caught by that task group's
    own ``__aexit__`` and re-raised as a
    ``BaseExceptionGroup("unhandled errors in a TaskGroup", ...)``,
    discarding this function's own carefully-written skip/failure message
    (ticket 035-011's finding against ``stdio_client``, confirmed to
    apply identically here). The caller instead calls this function
    inside its own local ``try/except BaseException``, stores whatever
    was raised, lets the ``async with`` block exit normally (no exception
    in flight -> no task-group wrapping), and re-raises the *original*
    exception object once it is closed -- outside the task group, so
    pytest sees the real ``Skipped``/``AssertionError`` with its real
    message intact.
    """
    guide = await session.call_tool("get_guide", {})
    assert guide.isError is False
    assert "AprilCam" in guide.content[0].text

    cameras = await session.call_tool("list_cameras", {})
    assert cameras.isError is False
    names = {c["name"] for c in cameras.structuredContent["result"]}
    assert arducam_slug in names, f"arducam not enumerated by the real daemon: {names}"

    # -- configure: push the playfield and camera-config link for
    # real, over the MCP tools, even though both were also
    # boot-loaded above -- proving set_playfield/set_camera_config
    # genuinely work against a real daemon, and (per the diagnostic
    # pass this ticket ran by hand) that re-pushing an identical
    # document does not disturb the boot-loaded calibration.
    set_pf = await session.call_tool("set_playfield", {"document": _main_playfield_document()})
    assert set_pf.isError is False, set_pf.content

    set_cc = await session.call_tool(
        "set_camera_config",
        {"document": {"name": arducam_slug, "playfield": "main-playfield", "settings": {}}},
    )
    assert set_cc.isError is False, set_cc.content

    # -- calibrate: a live attempt, honestly reported either way --
    cal = await session.call_tool("calibrate", {"camera": arducam_slug})
    if cal.isError:
        # A field-geometry failure (occlusion, bad focus, ...) is
        # not an MCP tool error -- calibrate() still returns a
        # normal CalibrationResult with succeeded=False, handled
        # below by the "confirm already calibrated" fallback.
        # isError=True instead means the *tool call itself*
        # raised -- on this rig, that is the camera failing to
        # open (enumeration/config, above, never proves this
        # process can actually open it -- docs/knowledge/daemon-
        # camera-grant-and-detection-priming.md). Only that
        # specific, known signature is treated as environmental;
        # any other tool-call error still fails the assert below
        # for real.
        error_text = cal.content[0].text if cal.content else repr(cal.content)
        if "failed to open camera" in error_text:
            pytest.skip(
                f"{arducam_slug!r} is configured against "
                f"main-playfield but the real daemon could not open "
                f"it just now -- {error_text}. Expected when this "
                f"process has no camera TCC grant (e.g. an agent "
                f"session -- see docs/knowledge/daemon-camera-grant-"
                f"and-detection-priming.md) or the device is "
                f"genuinely busy elsewhere; re-run from an operator "
                f"Terminal session to exercise this test for real."
            )
    assert cal.isError is False, cal.content
    live_result = cal.structuredContent
    print(f"\n[026-006 hardware] live calibrate() attempt: {live_result}")

    # -- ... or confirm already calibrated: this machine's own
    # real, previously-solved calibration, boot-loaded above,
    # must still be intact regardless of the live attempt's
    # outcome (UC-04: "a failed attempt leaves any existing good
    # calibration untouched").
    confirmed = await session.call_tool("get_calibration", {"camera": arducam_slug})
    assert confirmed.isError is False, confirmed.content
    confirmed_calibration = confirmed.structuredContent["result"]
    assert confirmed_calibration is not None, (
        "camera reports uncalibrated after this test's setup -- no real prior "
        "calibration was found on this machine for boot-loading (see "
        f"_find_real_prior_calibration) and the live calibrate() attempt above "
        f"also failed: {live_result}. Physically aim the arducam at an "
        "unobstructed main-playfield (all 10 ArUco ids visible) and re-run."
    )
    assert confirmed_calibration["playfield"] == "main-playfield"
    print(f"[026-006 hardware] confirmed calibration: {confirmed_calibration}")

    # -- perceive: tags in world coordinates. Not asserted to be
    # non-empty -- UC-10's own "empty field returns an empty
    # frame, not an error" contract, and exactly what a
    # partially-obstructed live view may honestly produce right
    # now; the point here is the tool call itself, not a specific
    # tag's presence (test_calibration_hardware.py's own
    # precedent for not pinning to one specific tag).
    tags = await session.call_tool("get_tags", {"camera": arducam_slug})
    assert tags.isError is False, tags.content
    world_tags = [t for t in (tags.structuredContent or {}).get("tags", []) if t.get("world")]
    print(f"[026-006 hardware] {len(world_tags)} tag(s) with world coordinates this burst")

    frame = await session.call_tool(
        "get_frame", {"camera": arducam_slug, "deskewed": True, "format": "base64"}
    )
    assert frame.isError is False, frame.content
    assert frame.content[0].type == "image"
    image_bytes = base64.b64decode(frame.content[0].data)
    assert len(image_bytes) > 0

    draw = await session.call_tool(
        "draw_marker",
        {
            "playfield": "main-playfield",
            "layer": "ticket-026-006",
            "id": "agent-marker",
            "x": 0.0,
            "y": 0.0,
            "label": "ticket 026-006 acceptance",
        },
    )
    assert draw.isError is False, draw.content

    listed = await session.call_tool(
        "list_annotations", {"playfield": "main-playfield", "layer": "ticket-026-006"}
    )
    assert listed.isError is False
    ids = {a["id"] for a in listed.structuredContent["annotations"]}
    assert "agent-marker" in ids, f"the marker just drawn was not read back: {ids}"

    cleared = await session.call_tool(
        "clear_layer", {"playfield": "main-playfield", "layer": "ticket-026-006"}
    )
    assert cleared.isError is False

    after_clear = await session.call_tool(
        "list_annotations", {"playfield": "main-playfield", "layer": "ticket-026-006"}
    )
    assert after_clear.structuredContent["annotations"] == []

    # Printed (not asserted) so a `-s` run leaves a durable trace
    # of the real hardware verification, per this ticket's
    # honesty requirement.
    print(
        f"\n[026-006 hardware] golden path OK: cameras={sorted(names)}, "
        f"live calibrate()={live_result}, "
        f"confirmed calibration={confirmed_calibration}, "
        f"tags_with_world={len(world_tags)}, "
        f"frame_bytes={len(image_bytes)}"
    )


# ---------------------------------------------------------------------------
# Part 5: the full-package, dynamic cv2-in-sys.modules sweep.
#
# Deliberately NOT the same check as tests/test_no_cv2_in_client_modules.py
# (an AST scan of each file's own literal `import cv2` statements -- it
# cannot see a transitive import, which is exactly how the bug this
# ticket found and fixed (see the module docstring) escaped detection for
# five prior tickets). This is what sprint.md's Test Strategy and this
# ticket's own acceptance criteria actually ask for: import every mcp/
# submodule, then check sys.modules. Run in a clean subprocess -- inside
# this same pytest process, other test modules (calibration/, daemon/,
# ...) legitimately import cv2 themselves, which would make an in-process
# check meaningless (a false pass or fail depending on test order).
# ---------------------------------------------------------------------------


def test_cv2_absent_from_sys_modules_after_importing_every_mcp_module():
    script = textwrap.dedent(
        """
        import importlib
        import pkgutil
        import sys

        import aprilcam.mcp as mcp_pkg

        for info in pkgutil.walk_packages(mcp_pkg.__path__, mcp_pkg.__name__ + "."):
            importlib.import_module(info.name)

        offenders = sorted(name for name in sys.modules if name == "cv2" or name.startswith("cv2."))
        assert not offenders, f"cv2 modules present after importing every mcp/ submodule: {offenders}"
        print("OK")
        """
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, (
        f"dynamic cv2 sweep failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "OK" in result.stdout
