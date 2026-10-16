"""Display-free tests for the intrinsics GUI (sprint 044-008)."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from aprilcam.intrinsics.interfaces import (
    BANNER_GOOD_ENOUGH, BANNER_KEEP_GOING, IntrinsicsFinishResult, IntrinsicsStatus,
)
from aprilcam.view import intrinsics_gui as gui
from aprilcam.view.intrinsics_model import (
    colour_for, connection_banner, decide_enter, exit_code_for, font_sizes, key_action,
    tile_colour,
)
from aprilcam.view.viewer import NoDisplayAvailable


def _st(cam, good=False):
    return IntrinsicsStatus(camera=cam, good=good, view_count=20, rms_px=0.5,
                            banner=BANNER_GOOD_ENOUGH if good else BANNER_KEEP_GOING)


class Daemon:
    def __init__(self, good=(), refuse=False, poll_raises=None):
        self.good, self.refuse, self.poll_raises = set(good), refuse, poll_raises
        self.finished, self.aborted = [], []

    def get_intrinsics_status(self, cam):
        if self.poll_raises:
            raise self.poll_raises
        return _st(cam, cam in self.good)

    def finish_intrinsics(self, cam, install, force=False):
        self.finished.append((cam, install, force))
        ok = not self.refuse
        return IntrinsicsFinishResult(installed=ok, reason=None if ok else "no",
                                      status=_st(cam, True))

    def abort_intrinsics(self, cam):
        self.aborted.append(cam)


def test_font_scaling():
    assert font_sizes(1080).banner == 120
    assert font_sizes(2160).banner == 240
    assert font_sizes(1080, 2).metric < font_sizes(1080, 1).metric
    assert font_sizes(10).banner >= 20  # floor


def test_colours_and_keys():
    assert colour_for("green") != colour_for("red")
    assert colour_for("nope") == colour_for("plain")
    assert tile_colour("good") != tile_colour("none")
    assert key_action("Return") == "enter" and key_action("KP_Enter") == "enter"
    assert key_action("Escape") == "escape" and key_action("a") is None


def test_decide_enter():
    assert decide_enter({"a": True, "b": False}).cameras == ("a",)
    d = decide_enter({"a": False})
    assert d.action == "notice" and d.cameras == ()
    assert decide_enter({}).action == "quit"
    assert exit_code_for(["a"], ["a"]) == 0 and exit_code_for(["a"], ["a", "b"]) == 1
    assert connection_banner(None) is None and "LOST" in connection_banner("x")


def test_enter_installs_good_only_never_forces():
    d = Daemon(good={"a"})
    c = gui.IntrinsicsController(d, ["a", "b"], install=False)
    c.step()
    c.request("enter")
    c.step()
    assert d.finished == [("a", True, False)]
    assert "run aprilcam camera calibrate a to apply" in "\n".join(c.snapshot().notice_lines)
    assert not c.finished  # no auto-quit
    assert c.owned == {"b"}


def test_enter_when_not_good_shows_notice_installs_nothing():
    d = Daemon()
    c = gui.IntrinsicsController(d, ["a"], install=False)
    c.step()
    c.request("enter")
    c.step()
    assert d.finished == [] and "not GOOD" in c.snapshot().notice_lines[0]
    assert not c.finished


def test_never_auto_quits_on_green_then_enter_quits():
    d = Daemon(good={"a"})
    c = gui.IntrinsicsController(d, ["a"], install=False)
    for _ in range(3):
        c.step()
    assert not c.finished and d.finished == []
    c.request("enter"); c.step()
    assert d.finished and not c.finished and c.exit_code() == 0
    c.request("enter"); c.step()
    assert c.finished


def test_install_flag_installs_once():
    d = Daemon(good={"a"})
    c = gui.IntrinsicsController(d, ["a"], install=True)
    c.step(); c.step()
    assert len(d.finished) == 1 and not c.finished


@pytest.mark.parametrize("cmd", ["escape", "close"])
def test_escape_and_close_abort(cmd):
    d = Daemon(good={"a"})
    c = gui.IntrinsicsController(d, ["a", "b"], install=False)
    c.step()
    c.request(cmd); c.step()
    assert c.finished and sorted(d.aborted) == ["a", "b"] and d.finished == []
    assert c.exit_code() == 1


def test_refused_install_stays_owned():
    d = Daemon(good={"a"}, refuse=True)
    c = gui.IntrinsicsController(d, ["a"], install=False)
    c.step(); c.request("enter"); c.step()
    assert c.owned == {"a"} and c.exit_code() == 1


def test_dropped_connection_sets_error_banner_not_crash():
    d = Daemon(poll_raises=RuntimeError("boom"))
    c = gui.IntrinsicsController(d, ["a"], install=False)
    c.step()
    assert c.snapshot().error == "boom"


def test_headless_raises_no_display(monkeypatch):
    monkeypatch.setattr(gui, "_headless_reason", lambda: "no $DISPLAY")
    with pytest.raises(NoDisplayAvailable):
        gui.run_intrinsics_gui(Daemon(), ["a"], install=False)


def test_run_aborts_owned_and_returns_code():
    d = Daemon()

    class Shell:
        def __init__(self, ctl): self.ctl = ctl
        def run(self): pass

    rc = gui.run_intrinsics_gui(d, ["a"], install=False, shell_factory=Shell)
    assert rc == 1 and d.aborted == ["a"]


def test_no_cv2_import():
    tree = ast.parse(Path(gui.__file__).read_text())
    mods = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    mods += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    assert not any(m and m.split(".")[0] == "cv2" for m in mods)
