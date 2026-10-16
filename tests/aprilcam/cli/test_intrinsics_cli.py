"""Tests for ``aprilcam.cli.intrinsics`` (sprint 044-007) with a fake daemon."""
from __future__ import annotations

import argparse
import dataclasses

import pytest

from aprilcam.cli import camera as camera_cli
from aprilcam.cli import intrinsics as cli
from aprilcam.errors import ConnectionFailed
from aprilcam.intrinsics.interfaces import (
    BANNER_GOOD_ENOUGH, BANNER_KEEP_GOING, IntrinsicsFinishResult, IntrinsicsStatus,
)


def _st(cam, good=False):
    return IntrinsicsStatus(
        camera=cam, good=good, view_count=20 if good else 3, rms_px=0.5,
        banner=BANNER_GOOD_ENOUGH if good else BANNER_KEEP_GOING,
        next_hint=None if good else "move closer")


class FakeDaemon:
    def __init__(self, good_after=None, refuse=False, poll_raises=None):
        self.good_after = good_after if good_after is not None else {}
        self.refuse = refuse
        self.poll_raises = poll_raises
        self.started, self.aborted, self.finished, self.polls = [], [], [], {}

    def start_intrinsics(self, camera, board=None):
        self.started.append((camera, board))
        return _st(camera)

    def get_intrinsics_status(self, camera):
        if self.poll_raises:
            raise self.poll_raises
        n = self.polls[camera] = self.polls.get(camera, 0) + 1
        return _st(camera, good=n > self.good_after.get(camera, 10 ** 9))

    def finish_intrinsics(self, camera, install, force=False):
        self.finished.append((camera, install, force))
        ok = install and not self.refuse
        return IntrinsicsFinishResult(
            installed=ok, reason=None if ok else "refused", status=_st(camera, True))

    def abort_intrinsics(self, camera):
        self.aborted.append(camera)
        return _st(camera)


class FakeDiscovery:
    def __init__(self, daemon):
        self.daemon = daemon

    def connect(self, host=None):
        return self.daemon


def _args(**kw):
    base = dict(camera=["a"], board=None, install=False, no_gui=True, daemon=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _run(daemon, args, enters=(), **kw):
    """enters: poll numbers (1-based) at which Enter is 'pressed'."""
    calls = {"n": 0}

    def enter_ready(_t):
        calls["n"] += 1
        if calls["n"] > 50:
            raise KeyboardInterrupt
        return calls["n"] in enters

    kw.setdefault("run_gui", lambda *a, **k: pytest.fail("gui must not run"))
    return cli.cmd_intrinsics(args, discovery_factory=lambda: FakeDiscovery(daemon),
                              enter_ready=enter_ready, interval=0, **kw)


def test_enter_installs_when_good_and_does_not_abort(capsys):
    d = FakeDaemon(good_after={"a": 1})
    assert _run(d, _args(), enters={2}) == 0
    assert d.finished == [("a", True, False)]
    assert d.aborted == []
    out = capsys.readouterr().out
    assert "GOOD ENOUGH" in out and "INSTALLED" in out


def test_never_auto_quits_on_green():
    # no Enter ever: loop runs until the harness's KeyboardInterrupt, then aborts
    d = FakeDaemon(good_after={"a": 0})
    assert _run(d, _args()) == 1
    assert d.finished == [] and d.aborted == ["a"] and d.polls["a"] > 5


def test_enter_ignored_when_not_good(capsys):
    d = FakeDaemon()
    assert _run(d, _args(), enters={1, 2}) == 1
    assert d.finished == []
    assert "not GOOD ENOUGH" in capsys.readouterr().out
    assert d.aborted == ["a"]


def test_install_flag_installs_once_when_good():
    d = FakeDaemon(good_after={"a": 2})
    assert _run(d, _args(install=True)) == 0
    assert d.finished == [("a", True, False)] and d.aborted == []


def test_install_flag_never_installs_not_good():
    d = FakeDaemon()
    assert _run(d, _args(install=True)) == 1
    assert d.finished == [] and d.aborted == ["a"]


def test_refused_install_stays_owned_and_is_aborted():
    d = FakeDaemon(good_after={"a": 0}, refuse=True)
    assert _run(d, _args(install=True)) == 1
    assert len(d.finished) == 1 and d.aborted == ["a"]


def test_abort_on_error(capsys):
    d = FakeDaemon(poll_raises=ConnectionFailed("x"))
    assert _run(d, _args()) == 1
    assert d.aborted == ["a"]


def test_two_cameras_start_and_abort_all():
    d = FakeDaemon()
    _run(d, _args(camera=["a", "b"], board="3x3:30:20:DICT_4X4_50"))
    assert d.started == [("a", "3x3:30:20:DICT_4X4_50"), ("b", "3x3:30:20:DICT_4X4_50")]
    assert sorted(d.aborted) == ["a", "b"]


def test_two_cameras_install_each_when_good():
    d = FakeDaemon(good_after={"a": 1, "b": 1})
    assert _run(d, _args(camera=["a", "b"], install=True)) == 0
    assert sorted(c for c, _, _ in d.finished) == ["a", "b"] and d.aborted == []


@pytest.mark.parametrize("bad", ["nonsense", "5x7:10:20:DICT_4X4_50", "5x7:37:28:ARUCO"])
def test_bad_board_exits_before_any_rpc(bad, capsys):
    class Boom:
        def connect(self, host=None):
            raise AssertionError("no RPC for a bad board")

    rc = cli.cmd_intrinsics(_args(board=bad), discovery_factory=lambda: Boom())
    assert rc != 0 and "--board" in capsys.readouterr().err


def test_headless_falls_back_to_text_with_notice(capsys):
    d = FakeDaemon(good_after={"a": 0})
    assert _run(d, _args(no_gui=False), enters={1}, headless_check=lambda: "no $DISPLAY") == 0
    assert "using text mode" in capsys.readouterr().err


def test_gui_stub_selects_text_mode(capsys):
    d = FakeDaemon(good_after={"a": 0})
    seen = []
    rc = _run(d, _args(no_gui=False), enters={1}, headless_check=lambda: None,
              run_gui=lambda *a, **k: seen.append(1) or None)
    assert seen == [1] and rc == 0
    assert "GUI not available" in capsys.readouterr().err


def test_gui_chosen_when_display_and_gui_returns_code():
    d = FakeDaemon()
    rc = _run(d, _args(no_gui=False), headless_check=lambda: None,
              run_gui=lambda daemon, cams, install: 0)
    assert rc == 0 and d.aborted == ["a"]  # still released if GUI left it owned


def test_registered_under_camera():
    p = argparse.ArgumentParser()
    camera_cli.add_subparser(p.add_subparsers())
    ns = p.parse_args(["camera", "intrinsics", "a", "b", "--no-gui", "--install"])
    assert ns.camera == ["a", "b"] and ns.no_gui and ns.install and ns.func is cli.cmd_intrinsics
