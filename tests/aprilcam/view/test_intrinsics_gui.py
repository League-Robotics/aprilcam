"""Display-free tests for the intrinsics GUI (sprint 044-008)."""
from __future__ import annotations

import ast
import io
import types
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


def _jpeg(size=(160, 90), colour=(40, 40, 40)):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "JPEG")
    return buf.getvalue()


def _st(cam, good=False):
    return IntrinsicsStatus(camera=cam, good=good, view_count=20, rms_px=0.5,
                            banner=BANNER_GOOD_ENOUGH if good else BANNER_KEEP_GOING)


class Daemon:
    def __init__(self, good=(), refuse=False, poll_raises=None):
        self.good, self.refuse, self.poll_raises = set(good), refuse, poll_raises
        self.finished, self.aborted = [], []

    frame_raises = None

    def get_frame(self, cam, deskewed=False, encoding="jpeg"):
        self.frame_calls = getattr(self, "frame_calls", 0) + 1
        if self.frame_raises:
            raise self.frame_raises
        return types.SimpleNamespace(data=_jpeg(), width=160, height=90)

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
    assert d.action == "confirm" and d.cameras == () and "Enter again" in d.notice
    assert decide_enter({"a": False}, armed=True).action == "force"
    assert decide_enter({"a": False}, armed=True).cameras == ("a",)
    assert decide_enter({"a": True, "b": False}, armed=True).action == "finish"
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
    assert d.finished == [] and "NOT good" in c.snapshot().notice_lines[0]
    assert not c.finished


def test_second_enter_forces_install_of_not_good():
    d = Daemon()
    t = [100.0]
    c = gui.IntrinsicsController(d, ["a"], install=False, clock=lambda: t[0])
    c.step()
    c.request("enter")
    c.step()
    t[0] += 3.0
    c.request("enter")
    c.step()
    assert d.finished == [("a", True, True)]
    assert c.installed == {"a"} and c.owned == set()


def test_second_enter_after_window_only_rearms():
    d = Daemon()
    t = [100.0]
    c = gui.IntrinsicsController(d, ["a"], install=False, clock=lambda: t[0])
    c.step()
    c.request("enter")
    c.step()
    t[0] += 30.0
    c.request("enter")
    c.step()
    assert d.finished == []


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


# -- sprint 046-003: frames + overlay ---------------------------------------
class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_frame_rate_limit_and_snapshot():
    clk = _Clock()
    d = Daemon()
    c = gui.IntrinsicsController(d, ["a"], install=False, clock=clk)
    c.step()
    c.step()  # same instant: no second fetch
    assert d.frame_calls == 1
    clk.t += 0.1
    c.step()
    assert d.frame_calls == 1
    clk.t += 0.13
    c.step()
    assert d.frame_calls == 2
    snap = c.snapshot()
    assert snap.frames["a"].startswith(b"\xff\xd8") and snap.frame_stale["a"] is False
    assert snap.statuses  # status polling unaffected


def test_frame_failure_keeps_last_and_recovers():
    clk = _Clock()
    d = Daemon()
    c = gui.IntrinsicsController(d, ["a"], install=False, clock=clk)
    c.step()
    good = c.snapshot().frames["a"]
    d.frame_raises = RuntimeError("camera busy")
    clk.t += 1
    c.step()
    snap = c.snapshot()
    assert snap.frames["a"] == good and snap.frame_stale["a"] is True
    assert snap.statuses and "camera busy" in snap.error
    d.frame_raises = None
    clk.t += 1
    c.step()
    snap = c.snapshot()
    assert snap.frame_stale["a"] is False and snap.error is None


def _model(levels):
    from aprilcam.view.intrinsics_model import build_camera_model

    counts = {n: v for n, v in zip(("NW", "N", "NE", "W", "C", "E", "SW", "S", "SE"), levels)}
    return build_camera_model(IntrinsicsStatus(camera="a", good=False, view_count=3,
                                               cell_coverage=counts))


def test_compose_grid_tint_outline_size():
    from aprilcam.view.intrinsics_overlay import (
        GRID_COLOUR, OUTLINE_COLOUR, compose_camera_image)
    from PIL import ImageColor

    model = _model([2, 0, 0, 0, 0, 0, 0, 0, 1])
    outline = (((100.0, 20.0), (140.0, 20.0), (140.0, 70.0), (100.0, 70.0)),)
    img = compose_camera_image(_jpeg((200, 100)), model, outline, (200, 100), (400, 400))
    assert img.size == (400, 200)  # aspect preserved, fit to width
    # grid lines at 1/3 / 2/3
    assert img.getpixel((133, 150))[:3] == ImageColor.getrgb(GRID_COLOUR)
    assert img.getpixel((267, 150))[:3] == ImageColor.getrgb(GRID_COLOUR)
    assert img.getpixel((300, 67))[:3] == ImageColor.getrgb(GRID_COLOUR)
    # NW cell good (green-tinted) vs untinted N cell; SE some (amber)
    nw, n, se = img.getpixel((8, 8)), img.getpixel((200, 8)), img.getpixel((392, 192))
    assert nw[1] > nw[0] + 40 and nw[1] > n[1] + 20
    assert se[0] > se[2] + 40
    # outline top edge: x 100..140 native -> 200..280, y 20 -> 40
    assert img.getpixel((250, 40))[:3] == ImageColor.getrgb(OUTLINE_COLOUR)


def test_compose_dimmed_and_decode_failure():
    from aprilcam.view.intrinsics_overlay import compose_camera_image

    model = _model([0] * 9)
    bright = compose_camera_image(_jpeg((100, 100), (200, 200, 200)), model, None, None, (100, 100))
    dim = compose_camera_image(_jpeg((100, 100), (200, 200, 200)), model, None, None, (100, 100),
                               dimmed=True)
    assert dim.getpixel((2, 2))[0] < bright.getpixel((2, 2))[0]
    out = compose_camera_image(b"garbage", model, None, None, (100, 100), fallback=bright)
    assert out.size == bright.size and out.getpixel((2, 2)) == dim.getpixel((2, 2))
    assert compose_camera_image(b"", model, None, None, (100, 100)).size[0] > 0


def test_frames_are_keyed_by_the_status_camera_slug_not_the_typed_token():
    """Regression (2026-10-16): ``aprilcam camera lens 4`` never showed the
    image because frames were stored under the typed token ("4") while the
    renderer looked them up by the slug the daemon's status reports."""

    class SlugDaemon(Daemon):
        def get_intrinsics_status(self, cam):
            return _st("arducam-ov9782-usb-camera", False)

    clk = _Clock()
    c = gui.IntrinsicsController(SlugDaemon(), ["4"], install=False, clock=clk)
    c.step()
    snap = c.snapshot()
    assert snap.statuses[0].camera == "arducam-ov9782-usb-camera"
    assert "arducam-ov9782-usb-camera" in snap.frames
    assert snap.frame_stale["arducam-ov9782-usb-camera"] is False
    assert "4" not in snap.frames


def test_column_layout_is_fixed_and_image_is_largest_fit():
    from aprilcam.view.intrinsics_model import column_layout, fit_rect, font_sizes

    fs = font_sizes(1080, 2)
    a = column_layout(960, 140, 960, 900, fs)
    b = column_layout(960, 140, 960, 900, fs)  # no inputs depend on notes
    assert a == b
    ix0, iy0, ix1, iy1 = a.image
    px0, py0, px1, py1 = a.panel
    assert ix1 < px0 and px1 <= 960 + 960 and abs((px1 - px0) - 0.3 * 960) <= 1
    assert iy0 == 140 and iy1 == 900  # full column height
    assert ix0 >= 960 and ix1 > ix0
    # slots stack top to bottom inside the panel, no overlap
    slots = [a.name, a.error_now, a.could_change, a.tilts[0], a.message]
    for lo, hi in zip(slots, slots[1:]):
        assert lo[3] <= hi[1]
    assert all(t[2] - t[0] == a.tilts[0][2] - a.tilts[0][0] for t in a.tilts)
    assert a.message[3] - a.message[1] >= 2 * fs.body
    # the aspect-fit of a 4:3 frame is the largest one the image box allows
    fx0, fy0, fx1, fy1 = fit_rect((640, 480), a.image)
    assert (fx1 - fx0 == ix1 - ix0) or (fy1 - fy0 == iy1 - iy0)
    # different limit moves nothing in the panel's x geometry
    assert column_layout(960, 140, 960, 800, fs).panel[0::2] == a.panel[0::2]


def test_panel_slots_never_overlap_for_longest_strings():
    from aprilcam.view.intrinsics_model import (
        LONGEST_COULD_CHANGE, LONGEST_ERROR_NOW, column_layout, fit_font_size,
        font_sizes, panel_metric_sizes)
    from aprilcam.view.intrinsics_model import _text_lines

    for screen_w, screen_h, cams in ((1280, 720, 2), (1280, 720, 1), (1920, 1080, 2),
                                     (800, 600, 2)):
        fs = font_sizes(screen_h, cams)
        col_w = screen_w // cams
        lay = column_layout(0, 120, col_w, screen_h - 150, fs)
        px0, _, px1, _ = lay.panel
        err_px, could_px = panel_metric_sizes(px1 - px0 - 4, fs)
        assert err_px <= fs.metric and could_px <= fs.metric * 2 // 3
        # the longest strings fit their slot at two lines
        for text, px, slot in ((LONGEST_ERROR_NOW, err_px, lay.error_now),
                               (LONGEST_COULD_CHANGE, could_px, lay.could_change)):
            lines = _text_lines(text, px, px1 - px0 - 4)
            if screen_w >= 1280:
                assert lines <= 2
            assert slot[3] - slot[1] >= lines * px
        slots = [lay.name, lay.error_now, lay.could_change, lay.tilts[0], lay.message]
        for lo, hi in zip(slots, slots[1:]):
            assert lo[3] <= hi[1]
        assert lay.message[3] <= lay.panel[3]


def test_fit_font_size_shrinks_to_fit_and_floors():
    from aprilcam.view.intrinsics_model import fit_font_size

    text = "x" * 40
    wide = fit_font_size([text], 2000, 50)
    narrow = fit_font_size([text], 200, 50)
    assert wide == 50 and 8 <= narrow < wide
    assert fit_font_size([text], 1, 50) == 8
