"""Tests for ``aprilcam.view.viewer`` (ticket 022-002).

Headless throughout, per the ticket's testing plan and this project's
GUI-test rule: :class:`PollLoop`/:func:`run_raw_view` are exercised
against a fake ``Window``/``Daemon`` pair (no real GUI toolkit
instantiated), and the headless-detection branch is triggered by
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
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import List, Optional

import pytest
from PIL import Image

from aprilcam.view import viewer
from aprilcam.types import ImageFrame, PixelPoint, TagFamily, TagFrame, TagId, TagRecord

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


def _image_frame(
    data: bytes = b"raw-bytes",
    *,
    frame_dark: bool = False,
    tags: Optional[TagFrame] = None,
) -> ImageFrame:
    return ImageFrame(
        data=data,
        width=4,
        height=4,
        encoding="jpeg",
        timestamp=0.0,
        frame_dark=frame_dark,
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
