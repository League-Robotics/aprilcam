"""Tests for ``aprilcam.view.viewer`` (streaming design, ticket 027-007;
supersedes the poll-driven design tickets 022-002/022-006 built and
this same test file used to exercise).

Headless throughout, per the ticket's testing plan and this project's
GUI-test rule: :class:`StreamLoop`/:func:`run_raw_view`/
:func:`run_playfield_view` are exercised against fake ``Window``/
``Daemon``/``ImageStream`` doubles (no real GUI toolkit instantiated,
no real socket), and the headless-detection branch is triggered by
monkeypatching the environment rather than by actually removing a
display. ``TkWindow.__init__``'s fast headless path is asserted to
short-circuit *before* ``tk.Tk()`` is ever called, by making a real
call an assertion failure. The one test that opens a genuine window
(``test_tkwindow_opens_shows_and_closes_a_real_window``) is marked
``@pytest.mark.camera`` so it is excluded from the baseline headless
run (``pytest -m 'not camera'``), matching this project's existing
convention for anything that needs real hardware/display -- it is not
about camera hardware specifically, but reuses the one marker the
baseline command already deselects.

**Timing discipline for the threaded ``StreamLoop``.** Most tests below
either need only a single streamed frame (no ordering race is possible
with just one) or use :class:`_ScriptedImageStream`'s ``delay=`` to
space frames out comfortably past ``_PUMP_INTERVAL_S`` (the module's
real sampling cadence), so the main thread's single-slot "latest frame"
cell is virtually guaranteed to be sampled between frames rather than
having an intermediate frame silently dropped -- an ordering guarantee
this module's own backpressure design does not otherwise promise (older
undisplayed frames are meant to be dropped; these tests simply avoid
racing that behaviour where they need deterministic frame-by-frame
content). ``StreamLoop``-level tests instead override ``pump_interval``
to a much smaller real value so they run quickly without weakening that
guarantee. The one true wall-clock throughput test
(``test_stream_display_throughput_measurably_exceeds_default_poll_hz``)
deliberately uses real, unmodified timing -- a fake/virtual clock would
not prove anything about actual responsiveness.

The ``run_playfield_view`` geometry section below reuses
``client/renderer.py``'s real ``Renderer`` (not a fake) for the
geometry-sensitive tests -- it is cv2-free and pure Pillow/math, so it
costs nothing to run headless, and using the real thing is what makes
the "moves right/up on screen as the tag moves east/north" check
(UC-18.S-1) a genuine end-to-end stream->render->display check rather
than one more unit test of the renderer in isolation (already covered
by ``tests/aprilcam/client/test_renderer.py``).
"""

from __future__ import annotations

import io
import itertools
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable, Iterator, List, Optional, Tuple

import numpy as np
import pytest
from PIL import Image

from aprilcam.calibration.geometry import world_to_deskewed_pixel
from aprilcam.client.renderer import Renderer
from aprilcam.daemon.interfaces import ImageStream
from aprilcam.errors import AprilCamError, NotCalibrated
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

#: A much smaller real pump interval than the module's default
#: (``_PUMP_INTERVAL_S`` = 0.05s), used only by the ``StreamLoop``-level
#: tests below to keep them fast -- a real, tiny sleep (not a no-op)
#: still actively yields the GIL each cycle, so the background reader
#: thread gets scheduled promptly rather than racing a busy main loop.
_FAST_PUMP_INTERVAL_S = 0.001

#: How long two scripted frames are spaced apart when a test needs both
#: to be individually observed (not silently dropped by the single-slot
#: cell) -- comfortably larger than the module's real
#: ``_PUMP_INTERVAL_S`` (0.05s) so the first is virtually always sampled
#: before the second replaces it.
_ORDERED_FRAME_DELAY_S = 0.15


@dataclass
class _FakeWindow(viewer.Window):
    """A :class:`viewer.Window` double.

    ``close_after_frames`` is what drives the "close the window" signal
    -- ``pump()`` starts reporting closed once at least that many
    frames have been ``show()``\\ n. ``None`` means "never closes on its
    own" -- used by tests that end the loop another way (a raised
    exception).
    """

    close_after_frames: Optional[int] = None
    shown: List[bytes] = field(default_factory=list)
    statuses: List[Optional[str]] = field(default_factory=list)
    tags_updates: List[Optional[Tuple[TagRecord, ...]]] = field(default_factory=list)
    closed: bool = False
    close_called: bool = False

    def show(self, data: bytes) -> None:
        self.shown.append(data)

    def set_status(self, text: Optional[str]) -> None:
        self.statuses.append(text)

    def update_tags(self, tags: Optional[Tuple[TagRecord, ...]]) -> None:
        """Overridden (not the inherited no-op) purely for test
        instrumentation -- records every call so tests can assert what
        :class:`viewer.StreamLoop` fed the window, per frame."""
        self.tags_updates.append(tags)

    def pump(self) -> bool:
        if self.close_after_frames is not None and len(self.shown) >= self.close_after_frames:
            self.closed = True
        return not self.closed

    def close(self) -> None:
        self.closed = True
        self.close_called = True


@dataclass
class _TimedFakeWindow(viewer.Window):
    """A :class:`viewer.Window` double that closes itself once
    *duration_s* of real wall-clock time have elapsed -- used only by
    the throughput test, which needs to measure real elapsed time
    against real display calls; a fake/virtual clock would not prove
    anything about actual wall-clock responsiveness."""

    duration_s: float
    shown: List[bytes] = field(default_factory=list)
    statuses: List[Optional[str]] = field(default_factory=list)
    closed: bool = False
    close_called: bool = False
    _start: float = field(default_factory=time.monotonic, repr=False)

    def show(self, data: bytes) -> None:
        self.shown.append(data)

    def set_status(self, text: Optional[str]) -> None:
        self.statuses.append(text)

    def pump(self) -> bool:
        if time.monotonic() - self._start >= self.duration_s:
            self.closed = True
        return not self.closed

    def close(self) -> None:
        self.closed = True
        self.close_called = True

    def elapsed(self) -> float:
        return time.monotonic() - self._start


class _ScriptedImageStream(ImageStream):
    """A scripted :class:`~aprilcam.daemon.interfaces.ImageStream`
    double: yields the given *frames* one at a time (with an optional
    *delay* between them -- see this module's docstring on ordering),
    then idles -- checking a stop flag frequently -- until ``close()``
    is called, simulating a live stream that would otherwise keep
    publishing forever. ``close()`` sets that flag, which is what
    proves a reader thread stops promptly once the window closes.
    """

    #: How often the "waiting for more frames" branch re-checks the
    #: stop flag -- deliberately much shorter than any test's own
    #: timing, so close() is noticed promptly.
    _IDLE_CHECK_S = 0.005

    def __init__(self, frames: Iterable[ImageFrame], *, delay: float = 0.0) -> None:
        self._frames = frames
        self._delay = delay
        self._stop = threading.Event()
        self.close_called = False

    def __iter__(self) -> Iterator[ImageFrame]:
        for frame in self._frames:
            if self._stop.is_set():
                return
            yield frame
            if self._delay:
                time.sleep(self._delay)
        while not self._stop.wait(self._IDLE_CHECK_S):
            pass

    def close(self) -> None:
        self.close_called = True
        self._stop.set()


class _BoomImageStream(ImageStream):
    """An :class:`ImageStream` double whose ``__iter__`` fails
    immediately -- for testing that a mid-view stream failure
    propagates out of :func:`viewer.run_raw_view`/
    :func:`viewer.run_playfield_view` and still closes the window (the
    streaming equivalent of the old poll-based ``_BoomDaemon``)."""

    def __init__(self) -> None:
        self.close_called = False

    def __iter__(self) -> Iterator[ImageFrame]:
        raise RuntimeError("camera disconnected")

    def close(self) -> None:
        self.close_called = True


@dataclass
class _FakeStreamDaemon:
    """A ``Daemon``-shaped double exposing only ``stream_images`` --
    all ``run_raw_view``/``run_playfield_view`` need now that neither
    calls ``get_frame``/``get_tags`` directly (see ``view/viewer.py``'s
    module docstring). ``get_tags`` is defined only to fail loudly if
    ``run_playfield_view`` ever calls it -- a stronger, more
    informative proof for this ticket's "never calls get_tags"
    acceptance criterion than a bare ``AttributeError`` would be.
    """

    stream: Optional[ImageStream] = None
    stream_images_raises: Optional[Exception] = None
    requested_camera: Optional[str] = None
    requested_deskewed: List[bool] = field(default_factory=list)

    def stream_images(self, camera: str, *, deskewed: bool = False) -> ImageStream:
        self.requested_camera = camera
        self.requested_deskewed.append(deskewed)
        if self.stream_images_raises is not None:
            raise self.stream_images_raises
        assert self.stream is not None, "test forgot to configure a stream"
        return self.stream

    def get_tags(self, camera: str) -> TagFrame:
        raise AssertionError(
            "run_playfield_view must not call get_tags -- read frame.tags "
            "directly off the streamed, already-deskewed ImageFrame"
        )


class _PassThroughRenderer:
    """A ``client.interfaces.Renderer`` double that returns the frame's
    own bytes unchanged -- for tests that only care about subscription
    wiring / status surfacing, not real pixel rendering."""

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
    recently on this camera," the condition raw-view display must be
    indifferent to."""
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


def _mixed_family_tag_frame() -> TagFrame:
    """A ``TagFrame`` carrying one AprilTag record and one ArUco record
    -- for asserting ``_make_playfield_converter``'s ``convert()``
    populates ``DisplayFrame.tags`` with the AprilTag record only
    (sprint 033 ticket 001)."""
    apriltag_record = TagRecord(
        tag=TagId(TagFamily.APRILTAG, 7),
        world=None,
        heading_rad=None,
        world_velocity=None,
        speed=None,
        timestamp=0.0,
        pixel=PixelPoint(1.0, 2.0),
        calibrated=False,
    )
    aruco_record = TagRecord(
        tag=TagId(TagFamily.ARUCO, 3),
        world=None,
        heading_rad=None,
        world_velocity=None,
        speed=None,
        timestamp=0.0,
        pixel=PixelPoint(9.0, 9.0),
        calibrated=False,
    )
    return TagFrame(tags=(apriltag_record, aruco_record), timestamp=0.0, frame_index=0)


def _tiny_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), color=(10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _bare_tag_record(**overrides) -> TagRecord:
    """A minimal ``TagRecord`` for sidebar-formatting tests -- every
    field defaults to "absent" so a test only has to override what it
    actually cares about."""
    fields = dict(
        tag=TagId(TagFamily.APRILTAG, 1),
        world=None,
        heading_rad=None,
        world_velocity=None,
        speed=None,
        timestamp=0.0,
        pixel=None,
        calibrated=False,
        yaw_rad=None,
    )
    fields.update(overrides)
    return TagRecord(**fields)


# ---------------------------------------------------------------------------
# Window.update_tags -- concrete no-op default (sprint 032 ticket 002),
# and TkWindow's sidebar row-formatting pure functions.
# ---------------------------------------------------------------------------


def test_window_update_tags_default_is_a_concrete_noop():
    """Acceptance criterion: a fake ``Window`` that does not override
    ``update_tags`` does not raise when called -- with either a real
    tag tuple or ``None`` -- the deliberate exception to this module's
    raise-``NotImplementedError`` convention (sprint.md Design
    Rationale)."""
    window = viewer.Window()
    tags = _tag_frame_with_one_tag().tags

    assert window.update_tags(None) is None
    assert window.update_tags(tags) is None


def test_format_tag_row_shows_placeholders_for_every_absent_field():
    """Row-content table (ticket Description): pixel x/y, world x/y,
    angle, and speed each independently fall back to ``"--"`` when the
    record does not carry them -- exercised here with a pure function,
    no real Tk widget needed."""
    record = _bare_tag_record(tag=TagId(TagFamily.APRILTAG, 3))

    row = viewer._format_tag_row(record)

    assert "AT3" in row
    assert row.count("--") == 6  # pixel x, pixel y, world x, world y, angle, speed


def test_format_tag_row_formats_present_numeric_fields_and_abbreviates_aruco():
    record = _bare_tag_record(
        tag=TagId(TagFamily.ARUCO, 9),
        world=WorldPoint(x=3.0, y=-4.0),
        speed=7.0,
        pixel=PixelPoint(x=100.0, y=50.0),
        calibrated=True,
        yaw_rad=math.radians(45.0),
    )

    row = viewer._format_tag_row(record)

    assert "AR9" in row  # ArUco id abbreviated distinctly from AprilTag
    assert "100.0" in row
    assert "50.0" in row
    assert "3.0" in row
    assert "-4.0" in row
    assert "45.0" in row
    assert "7.0" in row
    assert "--" not in row


def test_format_sidebar_text_orders_rows_by_tag_number_no_mobile_stationary_split():
    """Sprint.md Scope: one single ordered list, by ``tag.tag.number``
    -- no mobile/stationary split -- regardless of the order records
    are handed in."""
    high = _bare_tag_record(tag=TagId(TagFamily.APRILTAG, 5))
    low = _bare_tag_record(tag=TagId(TagFamily.APRILTAG, 1))

    text = viewer._format_sidebar_text((high, low))
    lines = text.splitlines()

    assert len(lines) == 3  # header + two rows
    assert "AT1" in lines[1]
    assert "AT5" in lines[2]


def test_format_sidebar_text_with_no_tags_is_header_only():
    """``None`` or an empty tuple both produce the header alone --
    "no tags currently detected" reads as an intentional, still-legible
    panel state, not a blank pane."""
    assert viewer._format_sidebar_text(None) == viewer._SIDEBAR_HEADER
    assert viewer._format_sidebar_text(()) == viewer._SIDEBAR_HEADER


# ---------------------------------------------------------------------------
# _TagRoster -- sticky per-tag-id live/ghost/drop bookkeeping (sprint 033
# ticket 002). Pure and Tk-free throughout: an injected fake clock, no
# real window anywhere in this section (the ticket's own acceptance
# criterion).
# ---------------------------------------------------------------------------


class _FakeClock:
    """A controllable monotonic-clock double for :class:`viewer._TagRoster`
    tests -- advances only when told to, so a test can cross the 30s
    ghost horizon without sleeping."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _tag_record(number: int, family: TagFamily = TagFamily.APRILTAG, **overrides) -> TagRecord:
    return _bare_tag_record(tag=TagId(family, number), **overrides)


def test_tag_roster_reports_a_present_tag_as_live():
    roster = viewer._TagRoster(clock=_FakeClock())
    record = _tag_record(4)

    roster.update((record,))

    assert roster.snapshot() == ((record, False),)


def test_tag_roster_ghosts_a_tag_immediately_on_the_first_absent_frame():
    """No multi-frame grace period -- one ``update()`` call with the tag
    missing is enough (ticket's own acceptance criterion)."""
    roster = viewer._TagRoster(clock=_FakeClock())
    record = _tag_record(4)
    roster.update((record,))

    roster.update(())  # the tag is simply absent from this frame's tags

    ((snapshot_record, ghosted),) = roster.snapshot()
    assert ghosted is True
    assert snapshot_record == record  # last-seen values retained, not blanked


def test_tag_roster_keeps_a_ghosted_tags_last_seen_values_until_dropped():
    clock = _FakeClock()
    roster = viewer._TagRoster(clock=clock)
    record = _tag_record(4, speed=7.0)
    roster.update((record,))
    roster.update(())

    clock.advance(viewer._GHOST_HORIZON_S - 1.0)
    roster.update(())

    ((snapshot_record, ghosted),) = roster.snapshot()
    assert ghosted is True
    assert snapshot_record.speed == 7.0


def test_tag_roster_does_not_drop_a_ghosted_tag_before_the_horizon_elapses():
    clock = _FakeClock()
    roster = viewer._TagRoster(clock=clock)
    roster.update((_tag_record(4),))
    roster.update(())

    clock.advance(viewer._GHOST_HORIZON_S - 0.01)
    roster.update(())

    assert len(roster.snapshot()) == 1


def test_tag_roster_drops_a_tag_once_the_ghost_horizon_elapses():
    clock = _FakeClock()
    roster = viewer._TagRoster(clock=clock)
    roster.update((_tag_record(4),))
    roster.update(())

    clock.advance(viewer._GHOST_HORIZON_S)
    roster.update(())

    assert roster.snapshot() == ()


def test_tag_roster_restores_a_reappearing_tag_to_live_with_refreshed_values():
    clock = _FakeClock()
    roster = viewer._TagRoster(clock=clock)
    stale = _tag_record(4, speed=1.0)
    roster.update((stale,))
    roster.update(())  # ghosted

    clock.advance(5.0)  # well before the 30s horizon
    fresh = _tag_record(4, speed=9.0)
    roster.update((fresh,))

    ((snapshot_record, ghosted),) = roster.snapshot()
    assert ghosted is False
    assert snapshot_record.speed == 9.0


def test_tag_roster_orders_live_and_ghosted_rows_together_by_tag_number():
    """Sprint.md's "no separate stale section": one ordered list, live
    and ghosted rows interleaved by tag number."""
    roster = viewer._TagRoster(clock=_FakeClock())
    low = _tag_record(1)
    high = _tag_record(5)
    roster.update((low, high))
    roster.update((high,))  # low goes absent this frame -> ghosted

    snapshot = roster.snapshot()

    assert [record.tag.number for record, _ in snapshot] == [1, 5]
    assert snapshot[0][1] is True  # tag 1: ghosted
    assert snapshot[1][1] is False  # tag 5: still live


def test_tag_roster_never_admits_an_aruco_record():
    """Defense in depth (ticket's own acceptance criterion): even if an
    ArUco record somehow reached this class despite ticket 001's own
    upstream filter, it is never tracked."""
    roster = viewer._TagRoster(clock=_FakeClock())

    roster.update((_tag_record(3, family=TagFamily.ARUCO),))

    assert roster.snapshot() == ()


@pytest.mark.camera
def test_tkwindow_with_sidebar_builds_a_side_panel_and_updates_it_from_tags():
    """A thin smoke-level check of the real Tk widget wiring (the
    ticket's own testing note: row-formatting logic is unit-tested
    directly above, with no Tk root at all; this is the one test that
    actually builds the ``tk.Text`` side panel and reads its content
    back). Not about camera hardware, but reuses the one marker this
    project's GUI-test rule already deselects from the baseline run.

    Sprint 033 ticket 002: a tag going absent must not clear its row --
    the sticky roster keeps it listed with its last-seen values, greyed
    via the real ``_GHOST_TK_TAG`` Tk text tag, until dropped. The
    roster's own live/ghost/drop transitions are proven above with no
    Tk root at all; this test proves only the rendering side -- that the
    grey tag actually lands on a real widget's line range, which no pure
    unit test can observe."""
    window = viewer.TkWindow("aprilcam sidebar test", show_sidebar=True)
    try:
        record = _bare_tag_record(
            tag=TagId(TagFamily.APRILTAG, 4),
            world=WorldPoint(x=1.0, y=2.0),
            speed=3.0,
            pixel=PixelPoint(x=10.0, y=20.0),
            calibrated=True,
            yaw_rad=0.0,
        )
        window.update_tags((record,))
        content = window._sidebar_text.get("1.0", "end")
        assert "AT4" in content
        assert window._sidebar_text.tag_ranges(viewer._GHOST_TK_TAG) == ()

        window.update_tags(None)
        ghosted_content = window._sidebar_text.get("1.0", "end")
        # Sticky roster: absent this frame, still listed with its
        # last-seen values -- not cleared -- and now rendered grey.
        assert "AT4" in ghosted_content
        assert window._sidebar_text.tag_ranges(viewer._GHOST_TK_TAG) != ()

        window.update_tags((record,))
        restored_content = window._sidebar_text.get("1.0", "end")
        assert "AT4" in restored_content
        # Reappeared -- restored to live, grey tag no longer applied.
        assert window._sidebar_text.tag_ranges(viewer._GHOST_TK_TAG) == ()
    finally:
        window.close()


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
# StreamLoop -- background reader thread, single-slot display, stop-on-
# close, thread cleanup, error propagation
# ---------------------------------------------------------------------------


def _no_reader_thread_alive() -> bool:
    return not any(t.name == viewer._READER_THREAD_NAME and t.is_alive() for t in threading.enumerate())


def test_stream_loop_shows_and_forwards_status_until_the_window_closes():
    window = _FakeWindow(close_after_frames=1)
    stream = _ScriptedImageStream([_image_frame(data=b"frame-1", frame_dark=True)])

    viewer.StreamLoop(
        window, stream, viewer._convert_raw_frame, pump_interval=_FAST_PUMP_INTERVAL_S
    ).run()

    assert window.shown == [b"frame-1"]
    assert window.statuses == ["frame dark -- image may be hard to see"]
    assert stream.close_called is True
    # closed via pump() reporting False -- StreamLoop itself never calls
    # Window.close() (that is run_raw_view's/run_playfield_view's job).
    assert window.closed is True
    assert window.close_called is False


def test_stream_loop_never_shows_a_frame_once_the_window_is_already_closed():
    window = _FakeWindow(close_after_frames=0)  # closed from the very first pump()
    stream = _ScriptedImageStream([_image_frame(data=b"should never be shown")])

    viewer.StreamLoop(
        window, stream, viewer._convert_raw_frame, pump_interval=_FAST_PUMP_INTERVAL_S
    ).run()

    assert window.shown == []


def test_stream_loop_stops_the_reader_thread_promptly_when_the_window_closes():
    """The ticket's core threading acceptance criterion: closing the
    window stops the background reader thread promptly, with none left
    running afterwards -- exercised against a stream that would
    otherwise never stop producing on its own (``itertools.cycle``)."""
    window = _FakeWindow(close_after_frames=1)
    stream = _ScriptedImageStream(itertools.cycle([_image_frame(data=b"x")]))

    viewer.StreamLoop(
        window, stream, viewer._convert_raw_frame, pump_interval=_FAST_PUMP_INTERVAL_S
    ).run()

    assert stream.close_called is True
    assert _no_reader_thread_alive()


def test_stream_loop_propagates_a_stream_error_and_stops():
    window = _FakeWindow(close_after_frames=None)
    stream = _BoomImageStream()

    with pytest.raises(RuntimeError, match="camera disconnected"):
        viewer.StreamLoop(
            window, stream, viewer._convert_raw_frame, pump_interval=_FAST_PUMP_INTERVAL_S
        ).run()

    assert window.shown == []
    assert stream.close_called is True
    assert _no_reader_thread_alive()


# ---------------------------------------------------------------------------
# StreamLoop -- bounded first-frame timeout (sprint 038, SUC-004): a
# stream that never delivers a first frame fails within a bounded,
# diagnosable timeout instead of StreamLoop.run pumping forever.
# ---------------------------------------------------------------------------


#: A short, real (not virtual-clock) timeout for the tests below --
#: comfortably larger than ``_FAST_PUMP_INTERVAL_S`` so several pump
#: cycles elapse before the bound fires, but small enough to keep the
#: test suite fast.
_SHORT_FIRST_FRAME_TIMEOUT_S = 0.05


def test_stream_loop_raises_first_frame_timeout_when_no_frame_ever_arrives():
    """Acceptance criterion: a stub ``ImageStream`` that never yields a
    frame causes ``StreamLoop.run`` to raise ``FirstFrameTimeout`` within
    the configured bound, with the stream closed and the reader thread
    joined before the exception propagates."""
    window = _FakeWindow(close_after_frames=None)  # never closes on its own
    stream = _ScriptedImageStream([])  # yields nothing -- idles until close()

    with pytest.raises(viewer.FirstFrameTimeout) as excinfo:
        viewer.StreamLoop(
            window,
            stream,
            viewer._convert_raw_frame,
            pump_interval=_FAST_PUMP_INTERVAL_S,
            camera="wedged-camera",
            first_frame_timeout_s=_SHORT_FIRST_FRAME_TIMEOUT_S,
        ).run()

    assert excinfo.value.camera == "wedged-camera"
    assert window.shown == []
    assert stream.close_called is True
    assert _no_reader_thread_alive()


def test_first_frame_timeout_is_not_an_aprilcam_error():
    """The ticket's own acceptance criterion, stated as literally as
    the class itself: ``FirstFrameTimeout`` must not be an
    ``aprilcam.errors.AprilCamError`` -- it is a client-local condition,
    mirroring ``NoDisplayAvailable``'s existing pattern, not a member of
    the daemon-facing error taxonomy."""
    assert not isinstance(viewer.FirstFrameTimeout("cam"), AprilCamError)


def test_stream_loop_shows_a_frame_promptly_when_within_a_short_timeout():
    """Regression: a stream that delivers its first frame within the
    bound is completely unaffected -- no timing regression to the
    ordinary pump/display path, even with a short injected
    ``first_frame_timeout_s``."""
    window = _FakeWindow(close_after_frames=1)
    stream = _ScriptedImageStream([_image_frame(data=b"frame-1")])

    viewer.StreamLoop(
        window,
        stream,
        viewer._convert_raw_frame,
        pump_interval=_FAST_PUMP_INTERVAL_S,
        first_frame_timeout_s=_SHORT_FIRST_FRAME_TIMEOUT_S,
    ).run()

    assert window.shown == [b"frame-1"]


def test_stream_loop_first_frame_timeout_never_fires_again_once_a_frame_arrived():
    """Acceptance criterion: once any frame has been received, the
    timeout logic never fires again for the rest of that stream's life
    -- proven by holding the window open, via real wall-clock time, well
    past ``first_frame_timeout_s`` after the one frame the stream ever
    yields, and confirming no ``FirstFrameTimeout`` is raised."""
    window = _TimedFakeWindow(duration_s=0.15)
    stream = _ScriptedImageStream([_image_frame(data=b"frame-1")])

    # Must not raise -- the window closes normally (via _TimedFakeWindow's
    # own real-time bound) even though it stays open several multiples of
    # _SHORT_FIRST_FRAME_TIMEOUT_S past the one frame ever shown.
    viewer.StreamLoop(
        window,
        stream,
        viewer._convert_raw_frame,
        pump_interval=_FAST_PUMP_INTERVAL_S,
        first_frame_timeout_s=_SHORT_FIRST_FRAME_TIMEOUT_S,
    ).run()

    assert window.shown == [b"frame-1"]
    assert window.close_called is False  # StreamLoop itself never calls close()


# ---------------------------------------------------------------------------
# run_raw_view -- UC-16 wiring: subscribes deskewed=False, raw bytes
# only, frame_dark advisory, no-display propagation, cleanup guarantee
# ---------------------------------------------------------------------------


def test_run_raw_view_subscribes_to_the_raw_stream_and_shows_bytes_unchanged():
    stream = _ScriptedImageStream([_image_frame(data=b"raw-1")])
    daemon = _FakeStreamDaemon(stream=stream)
    window = _FakeWindow(close_after_frames=1)

    viewer.run_raw_view(
        "arducam-ov9782-usb-camera", daemon, window_factory=lambda title: window
    )

    assert window.shown == [b"raw-1"]
    assert daemon.requested_camera == "arducam-ov9782-usb-camera"
    assert daemon.requested_deskewed == [False]
    assert window.close_called is True
    assert stream.close_called is True
    assert _no_reader_thread_alive()


def test_run_raw_view_window_title_carries_an_explicit_raw_marker():
    """Sprint 031 ticket 004 (SUC-004): the window title must state
    RAW explicitly, visible for the whole session -- not just the CLI's
    startup line. Captured via the ``window_factory`` injection seam,
    no real display needed."""
    stream = _ScriptedImageStream([_image_frame(data=b"raw-1")])
    daemon = _FakeStreamDaemon(stream=stream)
    window = _FakeWindow(close_after_frames=1)
    captured_titles: List[str] = []

    def capturing_factory(title: str) -> viewer.Window:
        captured_titles.append(title)
        return window

    viewer.run_raw_view("arducam-ov9782-usb-camera", daemon, window_factory=capturing_factory)

    assert len(captured_titles) == 1
    assert "RAW" in captured_titles[0]
    assert "arducam-ov9782-usb-camera" in captured_titles[0]


def test_run_raw_view_ignores_embedded_tags_even_when_detection_is_active():
    """UC-16 'raw means raw', taken literally: an embedded ``TagFrame``
    (an active, recently-queried ring buffer) must not change what
    gets displayed -- the exact bytes the daemon streamed, nothing
    drawn on top."""
    stream = _ScriptedImageStream(
        [_image_frame(data=b"raw-with-tags", tags=_tag_frame_with_one_tag())]
    )
    daemon = _FakeStreamDaemon(stream=stream)
    window = _FakeWindow(close_after_frames=1)

    viewer.run_raw_view("cam", daemon, window_factory=lambda title: window)

    assert window.shown == [b"raw-with-tags"]
    # Window.update_tags's inherited no-op default is what the raw view
    # exercises (sprint 032 ticket 002, SUC-002 acceptance criterion):
    # DisplayFrame.tags stays None regardless of the embedded TagFrame,
    # since _convert_raw_frame never reads frame.tags at all.
    assert window.tags_updates == [None]


def test_run_raw_view_window_factory_receives_no_show_sidebar_kwarg():
    """Regression (ticket 002 acceptance criterion): run_raw_view's own
    window_factory call site is unchanged -- a single positional title,
    no ``show_sidebar`` kwarg -- so ``TkWindow(title)`` behaves exactly
    as before this ticket."""
    stream = _ScriptedImageStream([_image_frame(data=b"raw-1")])
    daemon = _FakeStreamDaemon(stream=stream)
    window = _FakeWindow(close_after_frames=1)
    captured_kwargs: dict = {}

    def factory(title, **kwargs):
        captured_kwargs.update(kwargs)
        return window

    viewer.run_raw_view("cam", daemon, window_factory=factory)

    assert captured_kwargs == {}


def test_run_raw_view_still_displays_a_dark_frame_with_the_advisory_surfaced():
    stream = _ScriptedImageStream([_image_frame(data=b"dark-frame", frame_dark=True)])
    daemon = _FakeStreamDaemon(stream=stream)
    window = _FakeWindow(close_after_frames=1)

    viewer.run_raw_view("cam", daemon, window_factory=lambda title: window)

    assert window.shown == [b"dark-frame"]  # never withheld
    assert window.statuses == ["frame dark -- image may be hard to see"]


def test_run_raw_view_propagates_no_display_available_before_any_daemon_call():
    daemon = _FakeStreamDaemon(stream=_ScriptedImageStream([_image_frame()]))

    def boom_factory(title: str) -> viewer.Window:
        raise viewer.NoDisplayAvailable("no $DISPLAY or $WAYLAND_DISPLAY set")

    with pytest.raises(viewer.NoDisplayAvailable):
        viewer.run_raw_view("cam", daemon, window_factory=boom_factory)

    assert daemon.requested_camera is None


def test_run_raw_view_closes_the_window_even_when_the_stream_raises():
    window = _FakeWindow(close_after_frames=None)
    daemon = _FakeStreamDaemon(stream=_BoomImageStream())

    with pytest.raises(RuntimeError, match="camera disconnected"):
        viewer.run_raw_view("cam", daemon, window_factory=lambda title: window)

    assert window.close_called is True


def test_run_raw_view_propagates_first_frame_timeout_and_still_closes_the_window():
    """Acceptance criterion: ``run_raw_view`` propagates
    ``FirstFrameTimeout`` unchanged -- the window is closed in this
    function's existing ``finally``, exactly like any other stream
    failure (e.g. the ``_BoomImageStream`` case above)."""
    window = _FakeWindow(close_after_frames=None)
    stream = _ScriptedImageStream([])  # never yields a frame
    daemon = _FakeStreamDaemon(stream=stream)

    with pytest.raises(viewer.FirstFrameTimeout) as excinfo:
        viewer.run_raw_view(
            "wedged-cam",
            daemon,
            window_factory=lambda title: window,
            first_frame_timeout_s=_SHORT_FIRST_FRAME_TIMEOUT_S,
        )

    assert excinfo.value.camera == "wedged-cam"
    assert window.close_called is True
    assert stream.close_called is True
    assert _no_reader_thread_alive()


# ---------------------------------------------------------------------------
# run_playfield_view -- UC-18 wiring (ticket 027-007): subscribes
# deskewed=True only (no get_tags/stream_tags), no-raw-frame-fallback
# on NotCalibrated, calibration_stale surfacing, and the live
# stream->render->display geometry check.
# ---------------------------------------------------------------------------


def test_run_playfield_view_subscribes_to_the_deskewed_stream_and_shows_frames():
    stream = _ScriptedImageStream(
        [_image_frame(data=b"f1"), _image_frame(data=b"f2")], delay=_ORDERED_FRAME_DELAY_S
    )
    daemon = _FakeStreamDaemon(stream=stream)
    window = _FakeWindow(close_after_frames=2)

    viewer.run_playfield_view(
        "arducam-ov9782-usb-camera",
        daemon,
        renderer_factory=_PassThroughRenderer,
        window_factory=lambda title, **kwargs: window,
    )

    assert daemon.requested_deskewed == [True]
    assert daemon.requested_camera == "arducam-ov9782-usb-camera"
    assert window.shown == [b"f1", b"f2"]


def test_run_playfield_view_never_calls_get_tags_only_stream_images():
    """The ticket's central simplification (UC-18, ticket 027-007): a
    deskewed ``ImageFrame`` already embeds its own ``TagFrame``, so this
    view needs exactly one subscription and zero ``get_tags``/
    ``stream_tags`` calls. ``_FakeStreamDaemon.get_tags`` raises if ever
    called -- this test passing at all is the proof it was not."""
    stream = _ScriptedImageStream([_image_frame(data=b"f1")])
    daemon = _FakeStreamDaemon(stream=stream)
    window = _FakeWindow(close_after_frames=1)

    viewer.run_playfield_view(
        "cam",
        daemon,
        renderer_factory=_PassThroughRenderer,
        window_factory=lambda title, **kwargs: window,
    )

    assert daemon.requested_deskewed == [True]
    assert window.shown == [b"f1"]


def test_run_playfield_view_never_falls_back_to_a_raw_frame_when_uncalibrated():
    """UC-18.S-2: the error propagates and no ``deskewed=False`` request
    was ever made -- the mechanical proof that a raw-frame substitute
    cannot happen on this path (see the module docstring's "never fall
    back" note)."""
    daemon = _FakeStreamDaemon(stream_images_raises=NotCalibrated("'cam' is not calibrated"))
    window = _FakeWindow(close_after_frames=None)

    with pytest.raises(NotCalibrated):
        viewer.run_playfield_view(
            "cam",
            daemon,
            renderer_factory=_PassThroughRenderer,
            window_factory=lambda title, **kwargs: window,
        )

    assert daemon.requested_deskewed == [True]
    assert False not in daemon.requested_deskewed  # never asked for a raw stream
    assert window.shown == []  # the failed subscription never reached Window.show
    assert window.close_called is True  # cleanup still happens, exactly like run_raw_view


def test_run_playfield_view_surfaces_calibration_stale_as_a_status():
    """UC-18's stale-mid-view failure mode: surfaced, not silently
    ignored -- a visible status indicator is sufficient (the ticket does
    not require stopping the view)."""
    stream = _ScriptedImageStream([_image_frame(data=b"stale-frame", calibration_stale=True)])
    daemon = _FakeStreamDaemon(stream=stream)
    window = _FakeWindow(close_after_frames=1)

    viewer.run_playfield_view(
        "cam",
        daemon,
        renderer_factory=_PassThroughRenderer,
        window_factory=lambda title, **kwargs: window,
    )

    assert window.shown == [b"stale-frame"]  # still displayed, not withheld
    assert window.statuses[0] is not None
    assert "stale" in window.statuses[0].lower()


def test_run_playfield_view_still_surfaces_a_dark_frame_advisory():
    stream = _ScriptedImageStream([_image_frame(data=b"dark-frame", frame_dark=True)])
    daemon = _FakeStreamDaemon(stream=stream)
    window = _FakeWindow(close_after_frames=1)

    viewer.run_playfield_view(
        "cam",
        daemon,
        renderer_factory=_PassThroughRenderer,
        window_factory=lambda title, **kwargs: window,
    )

    assert window.statuses[0] is not None
    assert "dark" in window.statuses[0].lower()


def test_run_playfield_view_propagates_no_display_available_before_any_daemon_call():
    daemon = _FakeStreamDaemon(stream=_ScriptedImageStream([_image_frame()]))

    def boom_factory(title: str, **kwargs) -> viewer.Window:
        raise viewer.NoDisplayAvailable("no $DISPLAY or $WAYLAND_DISPLAY set")

    with pytest.raises(viewer.NoDisplayAvailable):
        viewer.run_playfield_view("cam", daemon, window_factory=boom_factory)

    assert daemon.requested_camera is None


def test_run_playfield_view_raises_first_frame_timeout_naming_the_camera():
    """``playfield view`` gets the same bounded first-frame wait as
    ``camera view`` (it previously constructed ``StreamLoop`` without a
    camera, so a stalled deskewed stream surfaced as ``FirstFrameTimeout``
    with an empty camera name, after the loop's default bound)."""
    window = _FakeWindow(close_after_frames=None)
    daemon = _FakeStreamDaemon(stream=_ScriptedImageStream([]))

    with pytest.raises(viewer.FirstFrameTimeout) as excinfo:
        viewer.run_playfield_view(
            "hd-usb-camera",
            daemon,
            renderer_factory=_PassThroughRenderer,
            window_factory=lambda title, **kwargs: window,
            first_frame_timeout_s=_SHORT_FIRST_FRAME_TIMEOUT_S,
        )

    assert excinfo.value.camera == "hd-usb-camera"
    assert window.close_called is True


def test_run_playfield_view_closes_the_window_even_when_the_stream_raises():
    window = _FakeWindow(close_after_frames=None)
    daemon = _FakeStreamDaemon(stream=_BoomImageStream())

    with pytest.raises(RuntimeError, match="camera disconnected"):
        viewer.run_playfield_view(
            "cam",
            daemon,
            renderer_factory=_PassThroughRenderer,
            window_factory=lambda title, **kwargs: window,
        )

    assert window.close_called is True


def test_run_playfield_view_stops_the_reader_thread_and_unsubscribes_once_the_window_closes():
    """Closing the window stops the stream and its reader thread
    promptly -- the streaming equivalent of the old poll design's
    "closing the window stops polling" guarantee, exercised against a
    stream that would otherwise never stop producing on its own."""
    stream = _ScriptedImageStream(itertools.cycle([_image_frame(data=b"f")]))
    daemon = _FakeStreamDaemon(stream=stream)
    window = _FakeWindow(close_after_frames=2)

    viewer.run_playfield_view(
        "cam",
        daemon,
        renderer_factory=_PassThroughRenderer,
        window_factory=lambda title, **kwargs: window,
    )

    assert stream.close_called is True
    assert _no_reader_thread_alive()


# ---------------------------------------------------------------------------
# bare mode / DisplayFrame.tags wiring (sprint 032 ticket 002, SUC-002/SUC-003)
# ---------------------------------------------------------------------------


class _BoomRendererFactory:
    """A ``renderer_factory`` double that fails the test outright if
    ever called -- the strongest available proof that ``bare=True``
    never constructs a renderer at all (ticket 002's own acceptance
    criterion, phrased exactly this way)."""

    def __call__(self):
        raise AssertionError("bare mode must never call the renderer factory")


def test_run_playfield_view_populates_display_frame_tags_in_the_default_annotated_mode():
    """SUC-002: DisplayFrame.tags is populated straight from the
    streamed frame's own embedded TagFrame -- the same one the renderer
    already reads, no second subscription -- and reaches the window via
    Window.update_tags on every displayed frame."""
    tag_frame = _tag_frame_with_one_tag()
    stream = _ScriptedImageStream([_image_frame(data=b"f1", tags=tag_frame)])
    daemon = _FakeStreamDaemon(stream=stream)
    window = _FakeWindow(close_after_frames=1)

    viewer.run_playfield_view(
        "cam",
        daemon,
        renderer_factory=_PassThroughRenderer,
        window_factory=lambda title, **kwargs: window,
    )

    assert window.tags_updates == [tag_frame.tags]


def test_run_playfield_view_populates_display_frame_tags_apriltag_family_only():
    """Sprint 033 ticket 001: ``_make_playfield_converter``'s
    ``convert()`` filters ``DisplayFrame.tags`` to AprilTag-family
    records only -- an ArUco record present in the streamed frame's own
    embedded ``TagFrame`` must never reach the sidebar."""
    tag_frame = _mixed_family_tag_frame()
    apriltag_record = tag_frame.tags[0]
    stream = _ScriptedImageStream([_image_frame(data=b"f1", tags=tag_frame)])
    daemon = _FakeStreamDaemon(stream=stream)
    window = _FakeWindow(close_after_frames=1)

    viewer.run_playfield_view(
        "cam",
        daemon,
        renderer_factory=_PassThroughRenderer,
        window_factory=lambda title, **kwargs: window,
    )

    assert window.tags_updates == [(apriltag_record,)]


def test_run_playfield_view_bare_mode_never_calls_the_renderer_factory():
    """Acceptance criterion, asserted the strong way: a renderer_factory
    that raises if called is passed in, and bare mode still succeeds --
    proof no ``Renderer`` is ever constructed on this path."""
    stream = _ScriptedImageStream([_image_frame(data=b"f1")])
    daemon = _FakeStreamDaemon(stream=stream)
    window = _FakeWindow(close_after_frames=1)

    viewer.run_playfield_view(
        "cam",
        daemon,
        renderer_factory=_BoomRendererFactory(),
        window_factory=lambda title, **kwargs: window,
        bare=True,
    )

    assert window.shown == [b"f1"]


def test_run_playfield_view_bare_mode_shows_unmodified_bytes_and_never_populates_tags():
    """``bare=True`` reproduces today's plain image-only mode: the
    stream's own bytes, unmodified, and DisplayFrame.tags stays at its
    None default even though the streamed frame carries a real
    TagFrame -- no overlay, no sidebar data, by construction."""
    tag_frame = _tag_frame_with_one_tag()
    stream = _ScriptedImageStream([_image_frame(data=b"bare-bytes", tags=tag_frame)])
    daemon = _FakeStreamDaemon(stream=stream)
    window = _FakeWindow(close_after_frames=1)

    viewer.run_playfield_view(
        "cam",
        daemon,
        renderer_factory=_BoomRendererFactory(),
        window_factory=lambda title, **kwargs: window,
        bare=True,
    )

    assert window.shown == [b"bare-bytes"]  # unmodified -- no Renderer call
    assert window.tags_updates == [None]  # DisplayFrame.tags never populated


def test_run_playfield_view_bare_mode_builds_the_window_without_a_sidebar():
    stream = _ScriptedImageStream([_image_frame(data=b"f1")])
    daemon = _FakeStreamDaemon(stream=stream)
    window = _FakeWindow(close_after_frames=1)
    captured_kwargs: dict = {}

    def factory(title, **kwargs):
        captured_kwargs.update(kwargs)
        return window

    viewer.run_playfield_view(
        "cam",
        daemon,
        renderer_factory=_BoomRendererFactory(),
        window_factory=factory,
        bare=True,
    )

    assert captured_kwargs == {"show_sidebar": False}


def test_run_playfield_view_default_mode_builds_the_window_with_a_sidebar():
    stream = _ScriptedImageStream([_image_frame(data=b"f1")])
    daemon = _FakeStreamDaemon(stream=stream)
    window = _FakeWindow(close_after_frames=1)
    captured_kwargs: dict = {}

    def factory(title, **kwargs):
        captured_kwargs.update(kwargs)
        return window

    viewer.run_playfield_view(
        "cam", daemon, renderer_factory=_PassThroughRenderer, window_factory=factory
    )

    assert captured_kwargs == {"show_sidebar": True}


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


def _red_edge_midpoint(center: WorldPoint) -> WorldPoint:
    """The midpoint of the tag's *back* edge (``back_right``-
    ``back_left``) -- one of the three sides ``client/renderer.py``'s
    red open "U" actually draws under ticket 032-001's v1-parity scheme
    (the *front* edge, at ``center.y + _TAG_HALF_CM``, is the one side
    that scheme never draws red -- see that ticket's
    ``test_render_never_draws_the_front_edge_red``). Matches
    ``tests/aprilcam/client/test_renderer.py``'s own back-edge sampling
    point (``test_render_draws_the_red_u_at_the_position_the_metadata_predicts``);
    same intent as this module's old ``_front_edge_midpoint`` (proving
    the viewer's own stream->render->display wiring does not lose or
    shift the drawn position), moved to a pixel the new scheme actually
    draws on."""
    return WorldPoint(center.x, center.y - _TAG_HALF_CM)


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
    exercised through the full stream->render->display path with the
    real ``client/renderer.py`` ``Renderer`` rather than calling
    ``Renderer.render()`` directly (that direct check is
    ``test_renderer.py``'s job; this one proves the viewer's own wiring
    does not lose or shift it)."""
    frame = _deskewed_frame_with_tag_at(WorldPoint(0.0, 0.0))
    stream = _ScriptedImageStream([frame])
    daemon = _FakeStreamDaemon(stream=stream)
    window = _FakeWindow(close_after_frames=1)

    viewer.run_playfield_view(
        "arducam",
        daemon,
        renderer_factory=Renderer,
        window_factory=lambda title, **kwargs: window,
    )

    image = _decode_png(window.shown[0])
    red_edge_midpoint = _red_edge_midpoint(WorldPoint(0.0, 0.0))
    predicted = world_to_deskewed_pixel(
        red_edge_midpoint, world_extent=(_GEO_WIDTH_CM, _GEO_HEIGHT_CM), px_per_cm=_GEO_PX_PER_CM
    )
    # World (0, 0)'s back-edge midpoint (a drawn red side under the new
    # 3-sided-U scheme, ticket 032-001) is directly below the image
    # centre by exactly the tag's half-width in pixels -- both land
    # squarely in the centre neighbourhood of a 500x400 image.
    assert abs(predicted.x - _GEO_WIDTH_PX / 2) < 1.0
    patch = _mean_color_near(image, predicted.x, predicted.y)
    background = np.array(_GEO_BACKGROUND, dtype=np.float64)
    assert np.linalg.norm(patch.astype(np.float64) - background) > 30


def test_run_playfield_view_moves_the_drawn_tag_right_and_up_as_the_physical_tag_moves_east_and_north():
    """The mirror-catching check (sprint.md Success Criteria), verified
    end-to-end through the full stream->render->display path across two
    successive streamed frames -- spaced by ``_ORDERED_FRAME_DELAY_S``
    so both are individually observed (see this module's docstring on
    timing discipline): a tag moved east is drawn further right on the
    second frame than the first; moved north, further up."""
    start = WorldPoint(0.0, 0.0)
    moved = WorldPoint(10.0, 8.0)  # east (+x) and north (+y)
    stream = _ScriptedImageStream(
        [_deskewed_frame_with_tag_at(start), _deskewed_frame_with_tag_at(moved)],
        delay=_ORDERED_FRAME_DELAY_S,
    )
    daemon = _FakeStreamDaemon(stream=stream)
    window = _FakeWindow(close_after_frames=2)

    viewer.run_playfield_view(
        "arducam",
        daemon,
        renderer_factory=Renderer,
        window_factory=lambda title, **kwargs: window,
    )

    assert len(window.shown) == 2
    before = _decode_png(window.shown[0])
    after = _decode_png(window.shown[1])

    extent = (_GEO_WIDTH_CM, _GEO_HEIGHT_CM)
    predicted_start = world_to_deskewed_pixel(
        _red_edge_midpoint(start), world_extent=extent, px_per_cm=_GEO_PX_PER_CM
    )
    predicted_moved = world_to_deskewed_pixel(
        _red_edge_midpoint(moved), world_extent=extent, px_per_cm=_GEO_PX_PER_CM
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
# Throughput -- the headless "visibly beats 2 Hz" success criterion
# ---------------------------------------------------------------------------


def test_stream_display_throughput_measurably_exceeds_default_poll_hz():
    """Headless throughput proof (ticket 027-007's "visibly beats 2 Hz"
    success criterion, reframed as a wall-clock throughput assertion
    since real camera hardware is blocked this sprint): a fast fake
    stream -- frames arriving far faster than ``DEFAULT_POLL_HZ`` would
    have polled -- drives a measurably higher effective display
    throughput than ``DEFAULT_POLL_HZ`` (2 Hz), over a fixed real
    wall-clock window. Deliberately uses real, unmodified timing (no
    fake clock/sleep injection) -- this is the one test whose entire
    point is proving something about actual wall-clock responsiveness.
    """
    stream = _ScriptedImageStream(itertools.cycle([_image_frame(data=b"fast")]))
    daemon = _FakeStreamDaemon(stream=stream)
    window = _TimedFakeWindow(duration_s=0.3)

    viewer.run_raw_view("cam", daemon, window_factory=lambda title: window)

    elapsed = window.elapsed()
    assert elapsed > 0.0
    throughput_hz = len(window.shown) / elapsed
    assert throughput_hz > viewer.DEFAULT_POLL_HZ
    # A comfortable margin -- not a fluke of a single lucky sample --
    # matching the module's ~20 Hz sampling cadence (_PUMP_INTERVAL_S)
    # over this window.
    assert len(window.shown) >= 4


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
