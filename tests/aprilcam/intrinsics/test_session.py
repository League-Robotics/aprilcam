"""Session and manager tests driven synchronously through an injected frame source."""
from __future__ import annotations

import numpy as np
import pytest

from aprilcam.calibration.lens import LensStore
from aprilcam.intrinsics.interfaces import (
    BANNER_GOOD_ENOUGH,
    STATE_ABORTED,
    STATE_COLLECTING,
    STATE_FAILED,
    STATE_FINISHED,
    IntrinsicsSessionActive,
    NoIntrinsicsSession,
)
from aprilcam.intrinsics.manager import IDLE_TIMEOUT_S, IntrinsicsManager
from aprilcam.intrinsics.session import MAX_READ_FAILURES, IntrinsicsSession
from tests.aprilcam.intrinsics.boards import SPEC, spread_frames


class Feed:
    """A frame source that replays a list, then returns None."""

    def __init__(self, frames, loop=False):
        self.frames = list(frames)
        self._orig = list(frames)
        self.loop = loop
        self.reads = 0

    def __call__(self):
        self.reads += 1
        if not self.frames and self.loop:
            self.frames = list(self._orig)
        return self.frames.pop(0) if self.frames else None


@pytest.fixture(scope="module")
def frames():
    return spread_frames(18)


def drive(session, n=None):
    """Run capture/solve steps synchronously until the feed is exhausted."""
    steps = 0
    while session.step():
        steps += 1
        session.maybe_solve()
        if n is not None and steps >= n:
            break
        if not session._frame_source.frames:
            break


def test_session_reaches_good_and_installs(frames):
    session = IntrinsicsSession("cam", SPEC, Feed(frames))
    drive(session)
    st = session.status()
    assert st.state == STATE_COLLECTING
    assert st.view_count >= 12
    assert st.board_visible
    installed = []
    result = session.finish(True, install_cb=installed.append)
    assert result.installed, result.reason
    assert "run calibrate" in result.reason
    assert result.status.state == STATE_FINISHED
    assert result.status.good and result.status.banner == BANNER_GOOD_ENOUGH
    model = installed[0]
    assert model.source == "measured"
    assert model.views == result.status.view_count
    assert model.board_spec["spec"] == str(SPEC)
    assert model.rms_px is not None and model.residual_px is not None
    assert model.correction_px is not None and model.installed_at > 0
    assert model.image_size == (1280, 720)


def test_finish_refuses_not_good_unless_forced():
    session = IntrinsicsSession("cam", SPEC, Feed(spread_frames(8, seed=3)))
    drive(session)
    cb = []
    result = session.finish(True, install_cb=cb.append)
    assert not result.installed and cb == []
    assert result.status.state == STATE_COLLECTING  # session stays open
    assert not session.ended
    if result.lens is not None:  # enough views to solve -> force installs
        forced = session.finish(True, force=True, install_cb=cb.append)
        assert forced.installed and len(cb) == 1


def test_finish_without_install_ends_and_writes_nothing(frames):
    session = IntrinsicsSession("cam", SPEC, Feed(frames))
    drive(session)
    cb = []
    result = session.finish(False, install_cb=cb.append)
    assert not result.installed and cb == []
    assert result.status.state == STATE_FINISHED and session.ended


def test_abort_installs_nothing(frames):
    session = IntrinsicsSession("cam", SPEC, Feed(frames))
    drive(session, n=10)
    st = session.abort()
    assert st.state == STATE_ABORTED and session.ended
    assert session.abort().state == STATE_ABORTED  # idempotent


def test_resolution_change_is_an_error_and_blocks_install(frames):
    small = np.zeros((480, 640), dtype=np.uint8)
    session = IntrinsicsSession("cam", SPEC, Feed(frames[:6] + [small]))
    for _ in range(6):
        assert session.step()
    assert session.step() is False
    st = session.status()
    assert st.state == STATE_FAILED
    assert any("resolution changed" in e for e in st.errors)
    cb = []
    result = session.finish(True, force=True, install_cb=cb.append)
    assert not result.installed and cb == []


def test_unreadable_camera_sets_error_instead_of_hanging():
    session = IntrinsicsSession("cam", SPEC, Feed([]))
    results = [session.step() for _ in range(MAX_READ_FAILURES)]
    assert results[-1] is False
    st = session.status()
    assert st.state == STATE_FAILED
    assert any("not delivering frames" in e for e in st.errors)


def test_frame_source_exception_counts_as_failed_read():
    def boom():
        raise RuntimeError("usb unplugged")

    session = IntrinsicsSession("cam", SPEC, boom)
    assert session.step() is True
    assert session.status().state == STATE_COLLECTING


def test_threaded_session_reaches_good(frames):
    """The real capture + solver threads, with pacing disabled."""
    import time

    session = IntrinsicsSession("cam", SPEC, Feed(frames, loop=True), frame_interval=0.0)
    session.start()
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not session.status().good:
            if session.status().state == STATE_FAILED:
                break
            time.sleep(0.1)
    finally:
        st = session.status()
    assert st.good, st
    result = session.finish(True, install_cb=lambda m: None)
    assert result.installed, (result.reason, st.errors)


# ---- manager --------------------------------------------------------------------


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make_manager(tmp_path, feeds=None, clock=None):
    store = LensStore(tmp_path)
    feeds = feeds if feeds is not None else {}
    mgr = IntrinsicsManager(
        frame_source_for=lambda slug: feeds.setdefault(slug, Feed([])),
        install=store.put,
        clock=clock or FakeClock(),
        threaded=False,
    )
    return mgr, store, feeds


def test_double_start_refused_and_fresh_after_end(tmp_path, frames):
    mgr, _, feeds = make_manager(tmp_path, {"cam": Feed(frames[:10])})
    mgr.start("cam")
    with pytest.raises(IntrinsicsSessionActive):
        mgr.start("cam")
    mgr.abort("cam")
    assert mgr.status("cam").state == STATE_ABORTED  # final status stays readable
    st = mgr.start("cam")
    assert st.view_count == 0 and st.state == STATE_COLLECTING


def test_status_unknown_camera_raises(tmp_path):
    mgr, _, _ = make_manager(tmp_path)
    with pytest.raises(NoIntrinsicsSession):
        mgr.status("nope")


def test_bad_board_spec_is_value_error(tmp_path):
    mgr, _, _ = make_manager(tmp_path)
    with pytest.raises(ValueError):
        mgr.start("cam", "garbage")


def test_idle_session_auto_aborts_but_polling_keeps_it(tmp_path):
    clock = FakeClock()
    mgr, _, _ = make_manager(tmp_path, clock=clock)
    mgr.start("cam")
    clock.now += IDLE_TIMEOUT_S - 1
    mgr.status("cam")  # a poll resets the timer
    clock.now += IDLE_TIMEOUT_S - 1
    assert mgr.check_idle() == []
    clock.now += 2
    assert mgr.check_idle() == ["cam"]
    st = mgr.status("cam")
    assert st.state == STATE_ABORTED
    assert any("no status poll" in e for e in st.errors)
    assert mgr.active_slugs() == []


def test_abort_all_aborts_every_session(tmp_path):
    mgr, _, _ = make_manager(tmp_path)
    mgr.start("a")
    mgr.start("b")
    mgr.abort_all()
    assert mgr.active_slugs() == []
    assert mgr.status("a").state == STATE_ABORTED


def test_manager_finish_installs_into_store(tmp_path, frames):
    mgr, store, feeds = make_manager(tmp_path, {"cam": Feed(frames)})
    mgr.start("cam")
    session = mgr._active["cam"][0]
    drive(session)
    result = mgr.finish("cam", True)
    assert result.installed
    model = store.load("cam")
    assert model is not None and model.source == "measured"
    assert mgr.active_slugs() == []


def test_session_fills_and_clears_board_outline():
    from aprilcam.intrinsics.interfaces import BoardDetection, BoardLayout

    square = ((10.0, 10.0), (50.0, 10.0), (50.0, 40.0), (10.0, 40.0), (30.0, 25.0))
    degenerate = ((5.0, 5.0), (5.0, 5.0))

    class FakeDetector:
        layout = BoardLayout.CURRENT

        def __init__(self):
            self.script = [
                [BoardDetection(corners=square, ids=(0, 1, 2, 3, 4),
                                marker_count=5, layout=BoardLayout.CURRENT),
                 BoardDetection(corners=degenerate, ids=(0, 1),
                                marker_count=1, layout=BoardLayout.CURRENT)],
                [],
            ]

        def detect(self, gray):
            return self.script.pop(0)

    blank = np.zeros((64, 96), dtype=np.uint8)
    session = IntrinsicsSession("cam", SPEC, Feed([blank, blank]), detector=FakeDetector())
    assert session.status().board_outline is None
    assert session.step()
    outline = session.status().board_outline
    assert outline is not None and len(outline) == 1  # degenerate hull skipped
    assert set(outline[0]) == {(10.0, 10.0), (50.0, 10.0), (50.0, 40.0), (10.0, 40.0)}
    assert session.step()
    st = session.status()
    assert st.board_outline is None and not st.board_visible
