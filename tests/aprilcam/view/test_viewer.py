"""Tests for ``aprilcam.view.viewer`` (ticket 022-002; ``run_playfield_view``
added by ticket 022-006).

Headless throughout, per the ticket's testing plan and this project's
GUI-test rule: :class:`PollLoop`/:func:`run_raw_view`/
:func:`run_playfield_view` are exercised against a fake
``Window``/``Daemon`` pair (no real GUI toolkit instantiated), and the
headless-detection branch is triggered by monkeypatching the
environment rather than by actually removing a display.
``TkWindow.__init__``'s fast headless path is asserted to short-circuit
*before* ``tk.Tk()`` is ever called, by making a real call an assertion
failure. The one test that opens a genuine window
(``test_tkwindow_opens_shows_and_closes_a_real_window``) is marked
``@pytest.mark.camera`` so it is excluded from the baseline headless
run (``pytest -m 'not camera'``), matching this project's existing
convention for anything that needs real hardware/display -- it is not
about camera hardware specifically, but reuses the one marker the
baseline command already deselects.

The ``run_playfield_view`` section below reuses ``client/renderer.py``'s
real ``Renderer`` (not a fake) for the geometry-sensitive tests -- it is
cv2-free and pure Pillow/math, so it costs nothing to run headless, and
using the real thing is what makes the "moves right/up on screen as the
tag moves east/north" check (UC-18.S-1, sprint.md's mirror-catching
Success Criterion) a genuine end-to-end poll->render->display check
rather than one more unit test of the renderer in isolation (already
covered by ``tests/aprilcam/client/test_renderer.py``).
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import pytest
from PIL import Image

from aprilcam.calibration.geometry import world_to_deskewed_pixel
from aprilcam.client.renderer import Renderer
from aprilcam.errors import NotCalibrated
from aprilcam.view import viewer
from aprilcam.types import (
    ImageFrame,
    PixelPoint,
    TagFamily,
    TagFrame,
    TagId,
    TagRecord,
    WorldPoint,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _FakeWindow(viewer.Window):
    """A :class:`viewer.Window` double.

    ``close_after_frames`` is what drives the "close the window" signal
    -- ``pump()`` starts reporting closed once at least that many
    frames have been ``show()``\\ n, regardless of exactly how many
    times :class:`PollLoop` happens to call ``pump()`` per tick (that
    internal cadence is deliberately not something these tests pin --
    see ``PollLoop._wait_for_next_tick``). ``None`` means "never closes
    on its own" -- used by tests that end the loop another way (a
    raised exception).
    """

    close_after_frames: Optional[int] = None
    shown: List[bytes] = field(default_factory=list)
    statuses: List[Optional[str]] = field(default_factory=list)
    closed: bool = False
    close_called: bool = False

    def show(self, data: bytes) -> None:
        self.shown.append(data)

    def set_status(self, text: Optional[str]) -> None:
        self.statuses.append(text)

    def pump(self) -> bool:
        if self.close_after_frames is not None and len(self.shown) >= self.close_after_frames:
            self.closed = True
        return not self.closed

    def close(self) -> None:
        self.closed = True
        self.close_called = True


class _FakeClock:
    """Advances only when ``sleep`` is called -- keeps ``PollLoop``
    tests instant and deterministic with no real waiting."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleep_calls = 0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleep_calls += 1
        self.now += seconds


@dataclass
class _FakeDaemon:
    """A ``Daemon``-shaped double exposing only ``get_frame`` -- all
    ``run_raw_view`` needs (see ``view/viewer.py``'s ``_fetch_raw``)."""

    frames: List[ImageFrame] = field(default_factory=list)
    requested_camera: Optional[str] = None
    requested_deskewed: List[bool] = field(default_factory=list)

    def get_frame(self, camera: str, *, deskewed: bool = False, encoding: str = "jpeg") -> ImageFrame:
        self.requested_camera = camera
        self.requested_deskewed.append(deskewed)
        index = min(len(self.requested_deskewed) - 1, len(self.frames) - 1)
        return self.frames[index]


class _BoomDaemon:
    """A ``Daemon`` double whose ``get_frame`` always fails -- for
    testing that :func:`viewer.run_raw_view` propagates a mid-view
    error and still closes the window."""

    def get_frame(self, camera: str, *, deskewed: bool = False, encoding: str = "jpeg") -> ImageFrame:
        raise RuntimeError("camera disconnected")


@dataclass
class _FakePlayfieldDaemon:
    """A ``Daemon``-shaped double exposing ``get_tags``/``get_frame`` --
    all ``run_playfield_view`` needs (see ``view/viewer.py``'s
    ``_fetch_playfield``). ``calls`` records call *order* across both
    methods, which is what the poll-sequencing acceptance criterion
    (get_tags before each get_frame) actually asserts on."""

    frames: List[ImageFrame] = field(default_factory=list)
    get_frame_raises: Optional[Exception] = None
    calls: List[str] = field(default_factory=list)
    requested_camera: Optional[str] = None
    requested_deskewed: List[bool] = field(default_factory=list)
    get_tags_calls: int = 0

    def get_tags(self, camera: str) -> TagFrame:
        self.calls.append("get_tags")
        self.get_tags_calls += 1
        self.requested_camera = camera
        return TagFrame(tags=(), timestamp=0.0, frame_index=self.get_tags_calls)

    def get_frame(self, camera: str, *, deskewed: bool = False, encoding: str = "jpeg") -> ImageFrame:
        self.calls.append("get_frame")
        self.requested_camera = camera
        self.requested_deskewed.append(deskewed)
        if self.get_frame_raises is not None:
            raise self.get_frame_raises
        index = min(len(self.requested_deskewed) - 1, len(self.frames) - 1)
        return self.frames[index]


class _PassThroughRenderer:
    """A ``client.interfaces.Renderer`` double that returns the frame's
    own bytes unchanged -- for tests that only care about poll
    sequencing / status surfacing, not real pixel rendering."""

    def render(self, frame: ImageFrame, *, layers=None, extra=()) -> bytes:
        return frame.data


def _image_frame(
    data: bytes = b"raw-bytes",
    *,
    frame_dark: bool = False,
    calibration_stale: bool = False,
    tags: Optional[TagFrame] = None,
) -> ImageFrame:
    return ImageFrame(
        data=data,
        width=4,
        height=4,
        encoding="jpeg",
        timestamp=0.0,
        frame_dark=frame_dark,
        calibration_stale=calibration_stale,
        tags=tags,
    )


def _tag_frame_with_one_tag() -> TagFrame:
    """A non-empty ``TagFrame`` -- stands in for "detection has run
    recently on this camera," the condition the first acceptance
    criterion requires the raw view to be indifferent to."""
    record = TagRecord(
        tag=TagId(TagFamily.APRILTAG, 7),
        world=None,
        heading_rad=None,
        world_velocity=None,
        speed=None,
        timestamp=0.0,
        pixel=PixelPoint(1.0, 2.0),
        calibrated=False,
    )
    return TagFrame(tags=(record,), timestamp=0.0, frame_index=0)


def _tiny_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color=(10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# PollLoop -- frame delivery cadence, stop-on-close, status forwarding
# ---------------------------------------------------------------------------


def test_poll_loop_fetches_and_shows_until_the_window_closes():
    window = _FakeWindow(close_after_frames=3)
    clock = _FakeClock()
    calls: List[int] = []

    def fetch() -> viewer.PolledFrame:
        calls.append(len(calls) + 1)
        return viewer.PolledFrame(data=f"frame-{len(calls)}".encode())

    loop = viewer.PollLoop(window, fetch, poll_hz=2.0, sleep=clock.sleep, clock=clock.clock)
    loop.run()

    assert calls == [1, 2, 3]
    assert window.shown == [b"frame-1", b"frame-2", b"frame-3"]
    # closed via pump() reporting False -- PollLoop itself never calls
    # Window.close() (that is run_raw_view's job, see below).
    assert window.closed is True
    assert window.close_called is False


def test_poll_loop_never_fetches_a_frame_after_the_window_is_already_closed():
    window = _FakeWindow(close_after_frames=0)  # closed from the very first pump()
    clock = _FakeClock()
    fetch_calls = 0

    def fetch() -> viewer.PolledFrame:
        nonlocal fetch_calls
        fetch_calls += 1
        return viewer.PolledFrame(data=b"should never be produced")

    viewer.PollLoop(window, fetch, sleep=clock.sleep, clock=clock.clock).run()

    assert fetch_calls == 0
    assert window.shown == []


def test_poll_loop_forwards_each_frames_status_alongside_its_bytes():
    window = _FakeWindow(close_after_frames=2)
    clock = _FakeClock()
    frames = iter(
        [
            viewer.PolledFrame(data=b"one", status=None),
            viewer.PolledFrame(data=b"two", status="frame dark -- image may be hard to see"),
        ]
    )

    viewer.PollLoop(window, lambda: next(frames), sleep=clock.sleep, clock=clock.clock).run()

    assert window.shown == [b"one", b"two"]
    assert window.statuses == [None, "frame dark -- image may be hard to see"]


def test_poll_loop_propagates_a_fetch_error_and_stops():
    window = _FakeWindow(close_after_frames=None)

    def fetch() -> viewer.PolledFrame:
        raise RuntimeError("camera disconnected")

    with pytest.raises(RuntimeError, match="camera disconnected"):
        viewer.PollLoop(window, fetch).run()

    assert window.shown == []


# ---------------------------------------------------------------------------
# Headless detection -- UC-16.S-3, no real GUI toolkit instantiated
# ---------------------------------------------------------------------------


def test_headless_reason_flags_a_missing_display_on_linux(monkeypatch):
    monkeypatch.setattr(viewer.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    assert viewer._headless_reason() is not None


def test_headless_reason_is_satisfied_once_display_is_set(monkeypatch):
    monkeypatch.setattr(viewer.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")

    assert viewer._headless_reason() is None


def test_headless_reason_is_a_noop_off_linux(monkeypatch):
    """macOS/Windows don't use $DISPLAY for their native GUI backends
    -- the toolkit-open attempt is what would catch a headless macOS
    session, not this cheap check (see the module docstring)."""
    monkeypatch.setattr(viewer.sys, "platform", "darwin")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    assert viewer._headless_reason() is None


def test_tkwindow_raises_no_display_available_without_touching_the_toolkit(monkeypatch):
    """The headless-detection branch, triggering the correct error --
    and provably without a real GUI toolkit instantiated: ``tk.Tk``
    is replaced with something that fails the test outright if called."""
    monkeypatch.setattr(viewer.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("tk.Tk() must not be called once headless is pre-detected")

    monkeypatch.setattr(viewer.tk, "Tk", _must_not_be_called)

    with pytest.raises(viewer.NoDisplayAvailable):
        viewer.TkWindow("test window")


# ---------------------------------------------------------------------------
# run_raw_view -- UC-16 wiring: raw bytes only, frame_dark advisory,
# no-display propagation, cleanup guarantee
# ---------------------------------------------------------------------------

#: Fast enough that the real ``time.sleep``/``time.monotonic`` these
#: tests leave as defaults never add meaningful wall-clock time.
_FAST_POLL_HZ = 1000.0


def test_run_raw_view_polls_get_frame_and_shows_raw_bytes_unchanged():
    daemon = _FakeDaemon(frames=[_image_frame(data=b"raw-1"), _image_frame(data=b"raw-2")])
    window = _FakeWindow(close_after_frames=2)

    viewer.run_raw_view(
        "arducam-ov9782-usb-camera",
        daemon,
        window_factory=lambda title: window,
        poll_hz=_FAST_POLL_HZ,
    )

    assert window.shown == [b"raw-1", b"raw-2"]
    assert daemon.requested_camera == "arducam-ov9782-usb-camera"
    assert daemon.requested_deskewed == [False, False]
    assert window.close_called is True


def test_run_raw_view_ignores_embedded_tags_even_when_detection_is_active():
    """UC-16 'raw means raw', taken literally: an embedded ``TagFrame``
    (an active, recently-queried ring buffer) must not change what
    gets displayed -- the exact bytes the daemon returned, nothing
    drawn on top."""
    daemon = _FakeDaemon(frames=[_image_frame(data=b"raw-with-tags", tags=_tag_frame_with_one_tag())])
    window = _FakeWindow(close_after_frames=1)

    viewer.run_raw_view("cam", daemon, window_factory=lambda title: window, poll_hz=_FAST_POLL_HZ)

    assert window.shown == [b"raw-with-tags"]


def test_run_raw_view_still_displays_a_dark_frame_with_the_advisory_surfaced():
    daemon = _FakeDaemon(frames=[_image_frame(data=b"dark-frame", frame_dark=True)])
    window = _FakeWindow(close_after_frames=1)

    viewer.run_raw_view("cam", daemon, window_factory=lambda title: window, poll_hz=_FAST_POLL_HZ)

    assert window.shown == [b"dark-frame"]  # never withheld
    assert window.statuses == ["frame dark -- image may be hard to see"]


def test_run_raw_view_propagates_no_display_available_before_any_daemon_call():
    daemon = _FakeDaemon(frames=[_image_frame()])

    def boom_factory(title: str) -> viewer.Window:
        raise viewer.NoDisplayAvailable("no $DISPLAY or $WAYLAND_DISPLAY set")

    with pytest.raises(viewer.NoDisplayAvailable):
        viewer.run_raw_view("cam", daemon, window_factory=boom_factory)

    assert daemon.requested_camera is None


def test_run_raw_view_closes_the_window_even_when_the_loop_raises():
    window = _FakeWindow(close_after_frames=None)

    with pytest.raises(RuntimeError, match="camera disconnected"):
        viewer.run_raw_view(
            "cam", _BoomDaemon(), window_factory=lambda title: window, poll_hz=_FAST_POLL_HZ
        )

    assert window.close_called is True


# ---------------------------------------------------------------------------
# run_playfield_view -- UC-18 wiring (ticket 022-006): get_tags-then-
# get_frame(deskewed=True) sequencing, no-raw-frame-fallback on
# NotCalibrated, calibration_stale surfacing, and the live
# poll->render->display geometry check.
# ---------------------------------------------------------------------------


def test_run_playfield_view_calls_get_tags_before_each_get_frame_deskewed_true():
    daemon = _FakePlayfieldDaemon(frames=[_image_frame(data=b"f1"), _image_frame(data=b"f2")])
    window = _FakeWindow(close_after_frames=2)

    viewer.run_playfield_view(
        "arducam-ov9782-usb-camera",
        daemon,
        renderer_factory=_PassThroughRenderer,
        window_factory=lambda title: window,
        poll_hz=_FAST_POLL_HZ,
    )

    assert daemon.calls == ["get_tags", "get_frame", "get_tags", "get_frame"]
    assert daemon.requested_deskewed == [True, True]
    assert daemon.requested_camera == "arducam-ov9782-usb-camera"
    assert window.shown == [b"f1", b"f2"]


def test_run_playfield_view_never_falls_back_to_a_raw_frame_when_uncalibrated():
    """UC-18.S-2: the error propagates and no ``deskewed=False`` request
    was ever made -- the mechanical proof that a raw-frame substitute
    cannot happen on this path (see ``_fetch_playfield``'s docstring)."""
    daemon = _FakePlayfieldDaemon(get_frame_raises=NotCalibrated("'cam' is not calibrated"))
    window = _FakeWindow(close_after_frames=None)

    with pytest.raises(NotCalibrated):
        viewer.run_playfield_view(
            "cam",
            daemon,
            renderer_factory=_PassThroughRenderer,
            window_factory=lambda title: window,
            poll_hz=_FAST_POLL_HZ,
        )

    assert daemon.calls == ["get_tags", "get_frame"]
    assert False not in daemon.requested_deskewed  # never asked for a raw frame
    assert window.shown == []  # the failed tick never reached Window.show
    assert window.close_called is True  # cleanup still happens, exactly like run_raw_view


def test_run_playfield_view_surfaces_calibration_stale_as_a_status():
    """UC-18's stale-mid-view failure mode: surfaced, not silently
    ignored -- a visible status indicator is sufficient (the ticket does
    not require stopping the view)."""
    daemon = _FakePlayfieldDaemon(frames=[_image_frame(data=b"stale-frame", calibration_stale=True)])
    window = _FakeWindow(close_after_frames=1)

    viewer.run_playfield_view(
        "cam",
        daemon,
        renderer_factory=_PassThroughRenderer,
        window_factory=lambda title: window,
        poll_hz=_FAST_POLL_HZ,
    )

    assert window.shown == [b"stale-frame"]  # still displayed, not withheld
    assert window.statuses[0] is not None
    assert "stale" in window.statuses[0].lower()


def test_run_playfield_view_still_surfaces_a_dark_frame_advisory():
    daemon = _FakePlayfieldDaemon(frames=[_image_frame(data=b"dark-frame", frame_dark=True)])
    window = _FakeWindow(close_after_frames=1)

    viewer.run_playfield_view(
        "cam",
        daemon,
        renderer_factory=_PassThroughRenderer,
        window_factory=lambda title: window,
        poll_hz=_FAST_POLL_HZ,
    )

    assert window.statuses[0] is not None
    assert "dark" in window.statuses[0].lower()


def test_run_playfield_view_propagates_no_display_available_before_any_daemon_call():
    daemon = _FakePlayfieldDaemon(frames=[_image_frame()])

    def boom_factory(title: str) -> viewer.Window:
        raise viewer.NoDisplayAvailable("no $DISPLAY or $WAYLAND_DISPLAY set")

    with pytest.raises(viewer.NoDisplayAvailable):
        viewer.run_playfield_view("cam", daemon, window_factory=boom_factory)

    assert daemon.calls == []


def test_run_playfield_view_closes_the_window_even_when_the_loop_raises():
    class _BoomPlayfieldDaemon:
        def get_tags(self, camera: str) -> TagFrame:
            return TagFrame(tags=(), timestamp=0.0, frame_index=0)

        def get_frame(self, camera: str, *, deskewed: bool = False, encoding: str = "jpeg") -> ImageFrame:
            raise RuntimeError("camera disconnected")

    window = _FakeWindow(close_after_frames=None)

    with pytest.raises(RuntimeError, match="camera disconnected"):
        viewer.run_playfield_view(
            "cam", _BoomPlayfieldDaemon(), window_factory=lambda title: window, poll_hz=_FAST_POLL_HZ
        )

    assert window.close_called is True


def test_run_playfield_view_stops_polling_once_the_window_closes():
    """Closing the window stops new poll ticks -- the fourth acceptance
    criterion's other half (opening starts them, closing stops them)."""
    daemon = _FakePlayfieldDaemon(
        frames=[_image_frame(data=b"f1"), _image_frame(data=b"f2"), _image_frame(data=b"f3")]
    )
    window = _FakeWindow(close_after_frames=2)

    viewer.run_playfield_view(
        "cam",
        daemon,
        renderer_factory=_PassThroughRenderer,
        window_factory=lambda title: window,
        poll_hz=_FAST_POLL_HZ,
    )

    assert daemon.get_tags_calls == 2  # not 3 -- no tick after the window reports closed


# -- the live geometry check, through the real Renderer -----------------

#: Deliberately simple numbers for hand-checking, matching
#: ``tests/aprilcam/client/test_renderer.py``'s own fixture style.
_GEO_WIDTH_PX, _GEO_HEIGHT_PX = 500, 400
_GEO_WIDTH_CM, _GEO_HEIGHT_CM = 100.0, 80.0
_GEO_PX_PER_CM = _GEO_WIDTH_PX / _GEO_WIDTH_CM  # 5.0
_GEO_BACKGROUND = (200, 200, 200)
_TAG_HALF_CM = 5.0


def _solid_deskewed_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (_GEO_WIDTH_PX, _GEO_HEIGHT_PX), _GEO_BACKGROUND).save(buf, format="PNG")
    return buf.getvalue()


def _front_edge_midpoint(center: WorldPoint) -> WorldPoint:
    """The midpoint of the tag's front edge -- a boundary *line*, so
    (unlike the tag's own centre) it is actually drawn-on pixel by
    ``_draw_tag``'s ``draw.polygon(..., outline=...)`` call, matching
    ``test_renderer.py``'s own sampling point."""
    return WorldPoint(center.x, center.y + _TAG_HALF_CM)


def _tag_record_at(center: WorldPoint) -> TagRecord:
    front_left = WorldPoint(center.x - _TAG_HALF_CM, center.y + _TAG_HALF_CM)
    front_right = WorldPoint(center.x + _TAG_HALF_CM, center.y + _TAG_HALF_CM)
    back_right = WorldPoint(center.x + _TAG_HALF_CM, center.y - _TAG_HALF_CM)
    back_left = WorldPoint(center.x - _TAG_HALF_CM, center.y - _TAG_HALF_CM)
    return TagRecord(
        tag=TagId(TagFamily.APRILTAG, 1),
        world=center,
        heading_rad=None,
        world_velocity=None,
        speed=None,
        timestamp=0.0,
        calibrated=True,
        yaw_rad=0.0,
        world_corners=(front_left, front_right, back_right, back_left),
    )


def _deskewed_frame_with_tag_at(center: WorldPoint) -> ImageFrame:
    record = _tag_record_at(center)
    return ImageFrame(
        data=_solid_deskewed_png(),
        width=_GEO_WIDTH_PX,
        height=_GEO_HEIGHT_PX,
        encoding="png",
        timestamp=0.0,
        deskewed=True,
        px_per_cm=_GEO_PX_PER_CM,
        world_extent=(_GEO_WIDTH_CM, _GEO_HEIGHT_CM),
        tags=TagFrame(tags=(record,), timestamp=0.0, frame_index=0),
    )


def _decode_png(png_bytes: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(png_bytes)).convert("RGB"))


def _mean_color_near(image: np.ndarray, x: float, y: float, *, radius: int = 3) -> np.ndarray:
    height, width = image.shape[:2]
    x0, x1 = max(0, int(x) - radius), min(width, int(x) + radius + 1)
    y0, y1 = max(0, int(y) - radius), min(height, int(y) + radius + 1)
    patch = image[y0:y1, x0:x1]
    assert patch.size > 0
    return patch.reshape(-1, patch.shape[-1]).mean(axis=0)


def test_run_playfield_view_draws_apriltag_1_at_the_image_centre_for_a_centred_world_position():
    """UC-18.S-1: AprilTag 1, at the field's surveyed centre (world
    (0, 0)), is drawn at the image centre -- the anchor-invariant check,
    now exercised through the full poll->render->display path with the
    real ``client/renderer.py`` ``Renderer`` rather than calling
    ``Renderer.render()`` directly (that direct check is
    ``test_renderer.py``'s job; this one proves the viewer's own wiring
    does not lose or shift it)."""
    frame = _deskewed_frame_with_tag_at(WorldPoint(0.0, 0.0))
    daemon = _FakePlayfieldDaemon(frames=[frame])
    window = _FakeWindow(close_after_frames=1)

    viewer.run_playfield_view(
        "arducam",
        daemon,
        renderer_factory=Renderer,
        window_factory=lambda title: window,
        poll_hz=_FAST_POLL_HZ,
    )

    image = _decode_png(window.shown[0])
    front_edge_midpoint = _front_edge_midpoint(WorldPoint(0.0, 0.0))
    predicted = world_to_deskewed_pixel(
        front_edge_midpoint, world_extent=(_GEO_WIDTH_CM, _GEO_HEIGHT_CM), px_per_cm=_GEO_PX_PER_CM
    )
    # World (0, 0)'s front-edge midpoint is directly above the image
    # centre by exactly the tag's half-width in pixels -- both land
    # squarely in the centre neighbourhood of a 500x400 image.
    assert abs(predicted.x - _GEO_WIDTH_PX / 2) < 1.0
    patch = _mean_color_near(image, predicted.x, predicted.y)
    background = np.array(_GEO_BACKGROUND, dtype=np.float64)
    assert np.linalg.norm(patch.astype(np.float64) - background) > 30


def test_run_playfield_view_moves_the_drawn_tag_right_and_up_as_the_physical_tag_moves_east_and_north():
    """The mirror-catching check (sprint.md Success Criteria / this
    ticket's second acceptance criterion), now verified end-to-end
    through the full poll->render->display path across two successive
    poll ticks: a tag moved east is drawn further right on the second
    frame than the first; moved north, further up."""
    start = WorldPoint(0.0, 0.0)
    moved = WorldPoint(10.0, 8.0)  # east (+x) and north (+y)
    daemon = _FakePlayfieldDaemon(
        frames=[_deskewed_frame_with_tag_at(start), _deskewed_frame_with_tag_at(moved)]
    )
    window = _FakeWindow(close_after_frames=2)

    viewer.run_playfield_view(
        "arducam",
        daemon,
        renderer_factory=Renderer,
        window_factory=lambda title: window,
        poll_hz=_FAST_POLL_HZ,
    )

    assert len(window.shown) == 2
    before = _decode_png(window.shown[0])
    after = _decode_png(window.shown[1])

    extent = (_GEO_WIDTH_CM, _GEO_HEIGHT_CM)
    predicted_start = world_to_deskewed_pixel(
        _front_edge_midpoint(start), world_extent=extent, px_per_cm=_GEO_PX_PER_CM
    )
    predicted_moved = world_to_deskewed_pixel(
        _front_edge_midpoint(moved), world_extent=extent, px_per_cm=_GEO_PX_PER_CM
    )
    assert predicted_moved.x > predicted_start.x  # east -> right on screen
    assert predicted_moved.y < predicted_start.y  # north -> up on screen (smaller pixel-y)

    background = np.array(_GEO_BACKGROUND, dtype=np.float64)

    def _drawn(image: np.ndarray, x: float, y: float) -> bool:
        patch = _mean_color_near(image, x, y)
        return bool(np.linalg.norm(patch.astype(np.float64) - background) > 30)

    # Frame 1: something is drawn at the start position...
    assert _drawn(before, predicted_start.x, predicted_start.y)
    # ...and nothing is drawn yet at the position the tag has not
    # reached (the two positions are far enough apart, relative to the
    # drawn tag's own footprint, that this is a real distinction).
    assert not _drawn(before, predicted_moved.x, predicted_moved.y)

    # Frame 2: the drawing has followed the tag to its new position...
    assert _drawn(after, predicted_moved.x, predicted_moved.y)
    # ...and is no longer at the old one.
    assert not _drawn(after, predicted_start.x, predicted_start.y)


# ---------------------------------------------------------------------------
# Real GUI smoke test -- deselected from the headless baseline
# ---------------------------------------------------------------------------


@pytest.mark.camera
def test_tkwindow_opens_shows_and_closes_a_real_window():
    """Opens a genuine tkinter window, shows a real decoded PNG, pumps
    once, and closes it. Not about camera hardware, but reuses the one
    marker ``pytest -m 'not camera'`` already deselects, per this
    project's GUI-test rule (anything needing a real window is
    ``@pytest.mark.camera`` or similarly marked)."""
    window = viewer.TkWindow("aprilcam view test")
    try:
        window.show(_tiny_png())
        window.set_status("test status")
        assert window.pump() is True
    finally:
        window.close()
