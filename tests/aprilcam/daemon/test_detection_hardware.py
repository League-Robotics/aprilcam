"""Hardware verification against ``main-playfield`` (ticket 021-007) --
the sprint's acceptance gate, closing out the whole
``tag-detection-and-tag-queries.md`` issue.

Everything in tickets 001-006 was built and tested against fixtures and
fakes. This module exercises the same code -- unmodified -- for real,
against a real ``arducam-ov9782-usb-camera`` aimed at the physical
``main-playfield`` (AprilTag 1 at centre, ArUco 1-8 perimeter + 9-10
internal, per ``config/aprilcam-old/playfields/main-playfield.json``).
Ground truth confirmed by hand before writing this module (see this
ticket's completion notes): AprilTag 1 plus ArUco 1, 2, 4, 5, 7, 10 are
reliably detected; ArUco 3, 6, 8, 9 are not currently in view.

Every test is ``@pytest.mark.camera`` and skips cleanly (module-wide,
via ``_skip_if_arducam_not_attached``) on a machine without that camera
attached -- ``uv run pytest -m "not camera"`` must be unaffected.

**Two test-construction patterns, matching precedent already in this
sprint's suite, not invented here:**

* Most tests use the ``daemon`` fixture: a private, real daemon stack
  (``daemon.__main__.build`` -- the exact assembly a real
  ``python -m aprilcam.daemon`` process wires together) reached through
  a real gRPC channel and ``client.daemon_client.GrpcDaemon`` (the same
  typed client the CLI itself uses), matching
  ``test_lifecycle_integration.py``'s pattern. Fresh per test function
  so no two tests ever contend over the same exclusively-held camera
  device.
* Two tests spawn the real ``aprilcam`` console script via
  ``subprocess``, matching ``tests/aprilcam/cli/test_cli_integration.py``'s
  pattern -- the acceptance criteria explicitly name CLI commands
  (``camera tags <camera>``, ``camera image 3``), and the RPC-level
  ``daemon`` fixture alone would not exercise the CLI's own argv
  parsing and output formatting.

**Digital occlusion, not physical occlusion -- read before changing the
gap test.** One acceptance criterion asks for ``get_tag_history`` on a
tag "moved out of frame and back" (occlude one and poll). This test
module runs in an unattended, terminal-only automation environment with
no physical actuator -- nothing here can move an object, cover a lens,
or unplug a cable in the room. ``test_get_tag_history_shows_explicit_gap_for_a_digitally_occluded_tag``
below substitutes the closest automatable equivalent that is still
genuinely real: every frame is a fresh, real capture from the real
sensor; for a short, deliberate window, a small rectangle covering
*only* AprilTag 1's last-known pixel region is painted black on that
otherwise-real, freshly-captured frame before it reaches the *real,
unmodified* preprocessing/detection pipeline -- so the "tag not
detected" result for those frames is a genuine detector outcome on
real (if partially doctored) pixels, not a fabricated ``TagFrame``.
This is disclosed here and in the test's own docstring, and flagged in
this ticket's completion notes as a scope decision a human may want to
re-verify by hand (physically covering a marker and polling
``aprilcam camera tags``).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

import cv2
import grpc
import numpy as np
import pytest

from aprilcam.camera.backends import CaptureBackend
from aprilcam.camera.backends.opencv_backend import OpenCVBackend
from aprilcam.camera.camera import Camera
from aprilcam.camera.identity import resolve_all
from aprilcam.client.daemon_client import GrpcDaemon
from aprilcam.config.directories import Directories
from aprilcam.daemon import __main__ as daemon_main
from aprilcam.daemon.detection import DetectionEngine
from aprilcam.proto import aprilcam_v2_pb2_grpc
from aprilcam.types import TagFamily, TagId

pytestmark = pytest.mark.camera

#: ticket 006's ground-truth camera: aimed at main-playfield on this Mac.
ARDUCAM_SLUG = "arducam-ov9782-usb-camera"

#: main-playfield.json's centre marker -- always in frame if the
#: arducam is genuinely aimed at the field (ticket 006's ground truth).
APRILTAG_1 = TagId(family=TagFamily.APRILTAG, number=1)


def _skip_if_arducam_not_attached() -> None:
    slugs = {identity.slug for identity in resolve_all().values()}
    if ARDUCAM_SLUG not in slugs:
        pytest.skip(
            f"{ARDUCAM_SLUG!r} not attached to this machine -- this "
            "module's acceptance criteria are checked against that "
            "camera aimed at main-playfield (ticket 021-007)"
        )


def _short_tmp_dir(prefix: str) -> Path:
    """A short, top-level ``/tmp`` directory -- unix domain sockets have
    a ~103-character path limit on macOS/Linux, and pytest's
    ``tmp_path`` nests too deeply (matches every other
    ``@pytest.mark.camera`` module's identically-named workaround:
    ``test_lifecycle_integration.py``, ``test_cli_integration.py``,
    ``test_service.py``)."""
    return Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))


# ---------------------------------------------------------------------------
# Private in-process daemon, reached over a real gRPC channel
# (test_lifecycle_integration.py's pattern)
# ---------------------------------------------------------------------------


@pytest.fixture
def daemon(tmp_path):
    """A private, real daemon (``daemon.__main__.build``), started and
    stopped fresh for each test function so no two tests ever contend
    over the same exclusively-held camera device. Never the real
    system daemon's directories -- ``tmp_path``/a short ``/tmp`` dir
    stand in, exactly like every other ``@pytest.mark.camera`` module.
    Yields a ready ``GrpcDaemon`` (the same typed client the CLI uses).
    """
    _skip_if_arducam_not_attached()
    short_dir = _short_tmp_dir("ac-007-")
    try:
        directories = Directories(
            config_dir=tmp_path / "config", state_dir=tmp_path / "state"
        )
        lock_path = short_dir / "daemon.lock"
        socket_path = short_dir / "control.sock"
        lifecycle = daemon_main.build(
            directories, lock_path=lock_path, socket_path=socket_path
        )
        lifecycle.start()
        try:
            channel = grpc.insecure_channel(f"unix:{socket_path}")
            grpc.channel_ready_future(channel).result(timeout=5)
            stub = aprilcam_v2_pb2_grpc.AprilCamV2Stub(channel)
            yield GrpcDaemon(stub, target="localhost")
        finally:
            lifecycle.stop()
    finally:
        shutil.rmtree(short_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# AC: get_tags detects AprilTag 1 and a plausible subset of ArUco 1-10
# ---------------------------------------------------------------------------


def test_get_tags_detects_apriltag_1_and_a_plausible_aruco_subset(daemon):
    frame = daemon.get_tags(ARDUCAM_SLUG)
    observed = [(r.tag.family.value, r.tag.number) for r in frame.tags]

    assert any(r.tag == APRILTAG_1 for r in frame.tags), (
        "AprilTag 1 (main-playfield's centre marker) was not detected -- "
        "check that the arducam is physically aimed at main-playfield "
        f"(observed this burst: {observed})"
    )

    aruco_ids = {r.tag.number for r in frame.tags if r.tag.family == TagFamily.ARUCO}
    # "full recall is not required -- real lighting/framing varies" per
    # this ticket's acceptance criterion: a low, generous minimum
    # (ticket 006's ground truth reliably clears this by 3x) rather
    # than requiring the full declared 1-10 set.
    assert len(aruco_ids) >= 2, (
        f"expected a plausible subset of main-playfield's ArUco 1-10, got "
        f"only {sorted(aruco_ids)} this burst (observed: {observed})"
    )


# ---------------------------------------------------------------------------
# AC: no negative tag id anywhere (ticket 003 Decision 2 -- no -(N+1) leak)
# ---------------------------------------------------------------------------


def test_no_negative_tag_ids_ever_appear(daemon):
    frame = daemon.get_tags(ARDUCAM_SLUG)
    assert frame.tags, "no tags detected this burst -- re-run with main-playfield in view"
    for record in frame.tags:
        assert record.tag.number >= 0, (
            f"negative tag id leaked to the client: {record.tag} -- ticket "
            "003's Decision 2 (no ArUco -(N+1) encoding) has regressed"
        )


# ---------------------------------------------------------------------------
# AC: every record honestly uncalibrated -- world=None, usable pixel_corners,
# never zeros dressed as world coordinates
# ---------------------------------------------------------------------------


def test_every_record_is_honestly_uncalibrated_with_usable_pixel_corners(daemon):
    frame = daemon.get_tags(ARDUCAM_SLUG)
    assert frame.tags, "no tags detected this burst -- re-run with main-playfield in view"
    for record in frame.tags:
        assert record.calibrated is False, f"{record.tag}: calibrated=True with no calibration subsystem wired"
        assert record.world is None, f"{record.tag}: world is not None on an uncalibrated camera -- {record.world}"
        assert record.world_corners is None, f"{record.tag}: world_corners populated while uncalibrated"

        corners = record.pixel_corners
        assert corners is not None and len(corners) == 4, f"{record.tag}: pixel_corners missing/malformed: {corners}"
        xs = {round(c.x, 1) for c in corners}
        ys = {round(c.y, 1) for c in corners}
        assert len(xs) > 1 or len(ys) > 1, (
            f"{record.tag}: pixel_corners look degenerate (all corners "
            f"coincide) -- {corners}"
        )
        assert any(c.x != 0.0 or c.y != 0.0 for c in corners), (
            f"{record.tag}: pixel_corners are all (0, 0) -- looks like a "
            "fabricated/default value, not a real detection"
        )


# ---------------------------------------------------------------------------
# AC: corner/hat order matches a known-orientation physical tag (real
# hardware cross-check of ticket 003's synthetic-only verified hypothesis)
# ---------------------------------------------------------------------------


def test_corner_order_matches_apriltag_1s_known_canonical_mounting(daemon):
    """Cross-checks ticket 003's verified corner-order hypothesis --
    "detected corner index 0 is always the marker's own canonical
    top-left corner, as drawn by ``generateImageMarker`` with zero
    rotation" -- against a real, physically-mounted tag rather than a
    synthetic image.

    Method: fetch a real frame and this burst's real ``pixel_corners``
    for AprilTag 1 (main-playfield's centre marker -- its physical
    mounting is directly visible in the captured frame, not just
    inferred), perspective-warp the *reported corner order* straight
    into a canonical square (corner 0 -> top-left, 1 -> top-right, 2 ->
    bottom-right, 3 -> bottom-left of the output), and compare that
    warped real image against ``cv2.aruco.generateImageMarker``'s
    canonical (0 degree) rendering of AprilTag 36h11 id 1 at each of
    the 4 possible 90 degree rotations. If the reported corner order
    truly tracks the marker's own print orientation the way ticket 003
    found synthetically, the 0 degree rotation must be the decisive
    best match -- not a coincidence, not a near-tie with 90/180/270.

    This is the same verification method ticket 003 used, just run
    against real captured pixels (real lens distortion, real focus,
    real JPEG compression) instead of a synthetic affine warp.
    """
    frame_response = daemon.get_frame(ARDUCAM_SLUG, deskewed=False, encoding="jpeg")
    tag_frame = daemon.get_tags(ARDUCAM_SLUG)
    record = next((r for r in tag_frame.tags if r.tag == APRILTAG_1), None)
    if record is None:
        pytest.skip(
            "AprilTag 1 not detected this particular burst -- ground truth "
            "says it should be reliably present; re-run"
        )

    img_bytes = np.frombuffer(frame_response.data, dtype=np.uint8)
    img = cv2.imdecode(img_bytes, cv2.IMREAD_GRAYSCALE)
    assert img is not None, "could not decode the captured frame"

    size = 240
    src = np.array([[c.x, c.y] for c in record.pixel_corners], dtype=np.float32)
    dst = np.array(
        [[0, 0], [size, 0], [size, size], [0, size]], dtype=np.float32
    )
    matrix = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(img, matrix, (size, size))
    _, warped_bin = cv2.threshold(warped, 127, 255, cv2.THRESH_BINARY)

    april_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    reference = cv2.aruco.generateImageMarker(april_dict, 1, size)
    _, reference_bin = cv2.threshold(reference, 127, 255, cv2.THRESH_BINARY)

    scores = [
        float(np.mean(warped_bin == np.rot90(reference_bin, k))) for k in range(4)
    ]
    best_rotation = int(np.argmax(scores))
    margin = scores[0] - max(s for i, s in enumerate(scores) if i != 0)

    assert best_rotation == 0, (
        "reported corner order does NOT reconstruct AprilTag 1's canonical "
        f"print orientation on real hardware -- best match was a "
        f"{best_rotation * 90} degree rotation of the reference, not 0 "
        f"(per-rotation agreement scores={scores}). Ticket 003's convention "
        "may not hold under real lens distortion; if this is failing, "
        "daemon/detection.py's corner handling needs the reorder fix this "
        "ticket's acceptance criteria call for, and vision/detector.py's "
        "docstring needs updating to match."
    )
    assert margin > 0.10, (
        f"0-degree match ({scores[0]:.3f}) is not decisively better than "
        f"the next-best rotation ({max(s for i, s in enumerate(scores) if i != 0):.3f}) "
        f"-- inconclusive rather than a clear confirmation (scores={scores})"
    )


# ---------------------------------------------------------------------------
# AC: camera image/tags 3 (a real registry number) and the same commands
# by slug address the same physical camera
# ---------------------------------------------------------------------------


def test_camera_number_and_slug_address_the_same_physical_camera(daemon):
    states = daemon.list_cameras()
    state = next((s for s in states if s.name == ARDUCAM_SLUG), None)
    assert state is not None, f"{ARDUCAM_SLUG!r} missing from list_cameras()"
    assert state.number is not None, f"{ARDUCAM_SLUG!r} has no assigned registry number"

    by_slug = daemon.get_tags(ARDUCAM_SLUG)
    by_number = daemon.get_tags(str(state.number))

    # frame_index is a monotonic per-slug counter (daemon/detection.py) --
    # two back-to-back bursts against the *same* physical camera/ring
    # buffer increment by exactly one, whichever token addressed it.
    # Two *different* cameras could never produce this relationship.
    assert by_number.frame_index == by_slug.frame_index + 1, (
        f"camera number {state.number} and slug {ARDUCAM_SLUG!r} do not "
        f"appear to address the same physical camera/ring buffer "
        f"(frame_index {by_slug.frame_index} then {by_number.frame_index})"
    )


# ---------------------------------------------------------------------------
# AC: idle daemon runs zero detections over a real 10 s window
# ---------------------------------------------------------------------------


def test_idle_daemon_runs_zero_detections_over_ten_seconds(daemon):
    """An idle daemon (camera open, zero get_tags/get_tag/get_tag_history
    calls in flight) does no detection work at all -- ``DetectionEngine
    .run_burst`` is only ever called from those three RPCs
    (daemon/detection.py's module docstring: "demand-driven, no
    background thread"), so a real 10 s window with none of them in
    flight must show the ring buffer completely unchanged.

    The hard, deterministic assertion is the buffer-length check (never
    flaky under system load, and directly proves "zero detection bursts
    ran" -- the AC's actual substance). Process CPU time is also
    sampled and printed, but only as an informational data point, not a
    hard assertion: this fixture's daemon runs *in-process* with pytest
    itself (sharing gRPC's own connection-management threads -- visible
    as "Other threads are currently calling into gRPC" noise in this
    test's stderr -- and the interpreter's own background housekeeping),
    so a raw CPU-seconds number measured this way is not a clean
    isolated reading of "daemon-process CPU" and asserting a tight bound
    on it produced exactly one false failure while developing this test
    (1.16s of combined pytest+gRPC-thread overhead over a 10s window,
    with the buffer-length check simultaneously proving zero detections
    ran). The clean, isolated measurement -- a real, separate
    ``python -m aprilcam.daemon`` subprocess sampled via ``ps`` before
    and after a real idle 10s window with no CLI queries running -- is
    recorded by hand as this ticket's regression-pinned number (dev-plan
    Stage 8) in this ticket's completion notes instead, exactly because
    it needs OS-level process isolation this in-process fixture cannot
    give it.
    """
    daemon.get_tags(ARDUCAM_SLUG)  # warm up: opens + caches the camera
    history_before = daemon.get_tag_history(ARDUCAM_SLUG)
    cpu_before = time.process_time()

    time.sleep(10.0)  # a real 10 s idle window -- no RPCs in flight

    cpu_after = time.process_time()
    history_after = daemon.get_tag_history(ARDUCAM_SLUG)

    assert len(history_after) == len(history_before), (
        f"the ring buffer grew from {len(history_before)} to "
        f"{len(history_after)} entries during a 10 s window with zero "
        "get_tags/get_tag/get_tag_history calls in flight -- something "
        "ran a detection burst on its own"
    )

    cpu_delta = cpu_after - cpu_before
    print(
        f"\n[informational only -- see docstring] in-process pytest+daemon "
        f"combined CPU over a real 10s wall-clock idle window: "
        f"{cpu_delta:.3f}s (includes gRPC's own connection-management "
        "threads and pytest's own overhead, not an isolated daemon-"
        "process reading; the regression-pinned number lives in this "
        "ticket's completion notes, measured against a real standalone "
        "`python -m aprilcam.daemon` subprocess instead)"
    )


# ---------------------------------------------------------------------------
# AC: get_tag_history shows an explicit gap for a tag that left frame and
# came back -- digital occlusion; see the module docstring
# ---------------------------------------------------------------------------


class _OccludingBackend(CaptureBackend):
    """Wraps a real :class:`CaptureBackend`; every ``read()`` is a
    genuinely fresh real capture, but for a configured set of call
    numbers, a rectangle is painted black over a configured pixel
    region before the frame is returned -- simulating one tag being
    physically covered, without moving anything. See the module
    docstring's "Digital occlusion, not physical occlusion" section for
    why this test module cannot instead move a real object."""

    def __init__(self, real: CaptureBackend) -> None:
        self._real = real
        self._region: Optional[Tuple[int, int, int, int]] = None
        self._occlude_on: Set[int] = set()
        self._call_count = 0

    @property
    def is_open(self) -> bool:
        return self._real.is_open

    def open(self) -> None:
        self._real.open()

    def close(self) -> None:
        self._real.close()

    def frame_size(self):
        return self._real.frame_size()

    def occlude_region(self, region: Tuple[int, int, int, int]) -> None:
        self._region = region

    def occlude_next(self, n: int) -> None:
        """Black out ``self._region`` on each of the next *n* real reads."""
        start = self._call_count + 1
        self._occlude_on |= set(range(start, start + n))

    def read(self):
        frame = self._real.read()
        self._call_count += 1
        if frame is not None and self._region is not None and self._call_count in self._occlude_on:
            x0, y0, x1, y1 = self._region
            frame = frame.copy()
            cv2.rectangle(frame, (x0, y0), (x1, y1), (0, 0, 0), thickness=-1)
        return frame


def test_get_tag_history_shows_explicit_gap_for_a_digitally_occluded_tag():
    _skip_if_arducam_not_attached()
    identities = resolve_all()
    index = next(
        (idx for idx, ident in identities.items() if ident.slug == ARDUCAM_SLUG), None
    )
    assert index is not None  # guaranteed by _skip_if_arducam_not_attached

    backend = _OccludingBackend(OpenCVBackend(index))
    camera = Camera(ARDUCAM_SLUG, backend)
    engine = DetectionEngine()
    camera.open()
    try:
        # -- frames 1-2: establish AprilTag 1 present, real captures ---
        frame1 = engine.run_burst(ARDUCAM_SLUG, camera)
        frame2 = engine.run_burst(ARDUCAM_SLUG, camera)
        present1 = any(r.tag == APRILTAG_1 for r in frame1.tags)
        present2 = any(r.tag == APRILTAG_1 for r in frame2.tags)
        if not (present1 and present2):
            pytest.skip(
                "AprilTag 1 not reliably detected in the first two bursts "
                "-- cannot exercise a present-to-gap transition without a "
                "reliably-present starting point; re-run"
            )

        # -- occlude AprilTag 1's last-known region for the next 2 reads
        record2 = next(r for r in frame2.tags if r.tag == APRILTAG_1)
        xs = [c.x for c in record2.pixel_corners]
        ys = [c.y for c in record2.pixel_corners]
        pad = 20
        region = (
            max(0, int(min(xs) - pad)),
            max(0, int(min(ys) - pad)),
            int(max(xs) + pad),
            int(max(ys) + pad),
        )
        backend.occlude_region(region)
        backend.occlude_next(2)

        frame3 = engine.run_burst(ARDUCAM_SLUG, camera)
        frame4 = engine.run_burst(ARDUCAM_SLUG, camera)

        # -- occlusion lifts: frames 5-6, real captures again -----------
        frame5 = engine.run_burst(ARDUCAM_SLUG, camera)
        frame6 = engine.run_burst(ARDUCAM_SLUG, camera)

        history = engine.history(ARDUCAM_SLUG)
        assert len(history) == 6, (
            f"expected 6 buffered frames (one per run_burst call), got "
            f"{len(history)} -- history must never be shortened"
        )

        present = [any(r.tag == APRILTAG_1 for r in f.tags) for f in history]
        assert present == [True, True, False, False, True, True], (
            f"expected present/absent pattern "
            f"[True, True, False, False, True, True] (present, present, "
            f"gap, gap, present, present), got {present}"
        )

        # The "gap" entries are real TagFrame records with real,
        # monotonically-increasing frame_index/timestamp -- an explicit
        # absence, not a dropped or interpolated slot.
        indices = [f.frame_index for f in history]
        assert indices == sorted(indices) and len(set(indices)) == 6, (
            f"frame_index values are not a clean monotonic sequence of 6 "
            f"distinct entries: {indices}"
        )
    finally:
        camera.close()


# ---------------------------------------------------------------------------
# AC: `aprilcam camera tags <camera>` output is legible and correctly shows
# the uncalibrated flag against a real camera -- real subprocess CLI,
# matching tests/aprilcam/cli/test_cli_integration.py's pattern
# ---------------------------------------------------------------------------


def _aprilcam_bin() -> Path:
    candidate = Path(sys.executable).parent / "aprilcam"
    if not candidate.exists():  # pragma: no cover - environment-dependent
        pytest.skip(f"no aprilcam console script next to {sys.executable}")
    return candidate


def _run(bin_path: Path, *args: str, env: Dict[str, str], timeout: float = 30.0):
    return subprocess.run(
        [str(bin_path), *args], capture_output=True, env=env, timeout=timeout
    )


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """Isolated environment for a real ``aprilcam`` subprocess --
    mirrors ``tests/aprilcam/cli/test_cli_integration.py``'s
    ``isolated_env`` fixture (duplicated locally: every
    ``@pytest.mark.camera`` module in this project defines its own
    short-tmp-dir/env fixture rather than sharing one via conftest --
    matches that module's and ``test_lifecycle_integration.py``'s own
    precedent)."""
    short_dir = _short_tmp_dir("ac-007-cli-")
    try:
        monkeypatch.setenv("APRILCAM_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("APRILCAM_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_dir))

        directories = Directories()
        if directories.socket_dir != short_dir / "aprilcam":
            pytest.skip(
                "this sandbox's /run/aprilcam is available and writable, "
                "so socket_dir cannot be isolated down to a private tmp "
                "root -- cannot safely isolate a real-subprocess CLI test "
                "from a real daemon here"
            )
        yield dict(os.environ)
    finally:
        shutil.rmtree(short_dir, ignore_errors=True)


def test_camera_tags_cli_output_is_legible_against_real_hardware(cli_env):
    _skip_if_arducam_not_attached()
    bin_path = _aprilcam_bin()
    env = cli_env

    result = _run(bin_path, "daemon", "start", env=env)
    assert result.returncode == 0, result.stderr
    try:
        # Settle the registry before trusting anything, per
        # test_cli_integration.py's documented startup-race note.
        probe = _run(bin_path, "camera", "probe", env=env)
        assert probe.returncode == 0, probe.stderr

        result = _run(bin_path, "camera", "tags", ARDUCAM_SLUG, env=env)
        assert result.returncode == 0, result.stderr
        out = result.stdout.decode()

        assert "apriltag" in out or "aruco" in out, (
            f"no recognizable tag family in CLI output:\n{out}"
        )
        assert "UNCALIBRATED" in out, f"missing the uncalibrated marker:\n{out}"
        assert "pixel=" in out, f"missing pixel coordinates:\n{out}"
        # Never a fabricated world position for an uncalibrated record
        # (UC-20.S-3's failure-mode requirement).
        assert "world=" not in out, f"a world position was printed for an uncalibrated record:\n{out}"
    finally:
        stop = _run(bin_path, "daemon", "stop", env=env)
        assert stop.returncode == 0, stop.stderr


_NUMBER_RE = re.compile(r"\[(\d+)\]\s+" + re.escape(ARDUCAM_SLUG) + r"\b")


def test_camera_tags_by_cli_number_and_slug_agree_on_real_hardware(cli_env):
    """Re-verifies ticket 002 against real hardware: ``camera tags 3``
    (an actual registry number on this machine) and
    ``camera tags arducam-ov9782-usb-camera`` must be the same physical
    camera -- closing the sprint-020 acceptance leg UC-25.S-5 could not
    verify without hands on hardware."""
    _skip_if_arducam_not_attached()
    bin_path = _aprilcam_bin()
    env = cli_env

    result = _run(bin_path, "daemon", "start", env=env)
    assert result.returncode == 0, result.stderr
    try:
        probe = _run(bin_path, "camera", "probe", env=env)
        assert probe.returncode == 0, probe.stderr

        listing = _run(bin_path, "camera", "list", env=env)
        assert listing.returncode == 0, listing.stderr
        list_out = listing.stdout.decode()
        match = _NUMBER_RE.search(list_out)
        assert match is not None, f"{ARDUCAM_SLUG!r} not found in camera list output:\n{list_out}"
        number = match.group(1)

        by_slug = _run(bin_path, "camera", "tags", ARDUCAM_SLUG, env=env)
        by_number = _run(bin_path, "camera", "tags", number, env=env)
        assert by_slug.returncode == 0, by_slug.stderr
        assert by_number.returncode == 0, by_number.stderr

        # Both address the same physical camera aimed at main-playfield
        # -- AprilTag 1 must appear via either token (allowing for
        # ordinary frame-to-frame detector variation between the two
        # separate CLI invocations, not a different camera entirely).
        assert b"apriltag 1" in by_slug.stdout, by_slug.stdout
        assert b"apriltag 1" in by_number.stdout, by_number.stdout
    finally:
        stop = _run(bin_path, "daemon", "stop", env=env)
        assert stop.returncode == 0, stop.stderr
