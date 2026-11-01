"""Sprint 047 ticket 008: main thread pumps the run loop on macOS."""

from __future__ import annotations

import threading

from aprilcam.daemon.__main__ import _wait_for_shutdown


def test_darwin_pumps_until_shutdown():
    ev = threading.Event()
    calls = []

    def pump(s):
        calls.append(s)
        if len(calls) == 3:
            ev.set()

    _wait_for_shutdown(ev, platform="darwin", pump=pump, slice_s=0.01)
    assert calls == [0.01] * 3


def test_darwin_without_pyobjc_falls_back_to_plain_wait():
    ev = threading.Event()
    ev.set()

    def pump(s):
        raise ImportError("no Foundation")

    ev2 = threading.Event()
    threading.Timer(0.05, ev2.set).start()
    _wait_for_shutdown(ev2, platform="darwin", pump=pump)
    assert ev2.is_set()


def test_other_platforms_never_pump():
    ev = threading.Event()
    ev.set()

    def pump(s):
        raise AssertionError("must not pump")

    _wait_for_shutdown(ev, platform="linux", pump=pump)
