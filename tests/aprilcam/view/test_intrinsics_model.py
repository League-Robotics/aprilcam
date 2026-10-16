"""Pure rendering tests for ``aprilcam.view.intrinsics_model`` (sprint 044-007)."""
from aprilcam.intrinsics.interfaces import (
    BANNER_GOOD_ENOUGH, BANNER_KEEP_GOING, BANNER_SHOW_BOARD,
    IntrinsicsFinishResult, IntrinsicsStatus,
)
from aprilcam.view.intrinsics_model import (
    build_view_model, finish_text_lines, status_text_lines,
)

CELLS = dict(NW=2, N=1, NE=0, W=0, C=3, E=0, SW=0, S=0, SE=0)
TILTS = dict(N=1, E=0, S=0, W=2)


def show(cam="a"):
    return IntrinsicsStatus(camera=cam, banner=BANNER_SHOW_BOARD)


def keep(cam="a", **kw):
    base = dict(camera=cam, banner=BANNER_KEEP_GOING, view_count=8, tilted_count=3,
                cell_coverage=CELLS, tilt_coverage=TILTS, rms_px=0.8,
                next_hint="tilt the board left", last_reject="moving too fast")
    base.update(kw)
    return IntrinsicsStatus(**base)


def good(cam="a"):
    return keep(cam, banner=BANNER_GOOD_ENOUGH, good=True, view_count=20,
                residual_px=2.5, correction_px=9.0, rms_px=0.4, next_hint=None)


def test_show_board_banner():
    m = build_view_model([show()])
    assert m.banner_text == "SHOW ME THE BOARD" and m.banner_class == "dim"
    c = m.cameras[0]
    assert c.error_now == "error: need 6 more views"
    assert c.could_change == "could still change by: --"
    assert all(t.level == "none" for t in c.cells)


def test_keep_going_tiles_and_strings():
    m = build_view_model([keep()])
    assert m.banner_text == "KEEP GOING" and m.banner_class == "yellow"
    c = m.cameras[0]
    assert c.error_now == "error now: 0.80 px"
    assert "need 4 more views" in c.could_change
    assert [t.level for t in c.cells][:5] == ["good", "some", "none", "none", "good"]
    assert [t.name for t in c.tilts] == ["N", "E", "S", "W"]
    assert [t.level for t in c.tilts] == ["some", "none", "none", "good"]
    assert m.next_line == "tilt the board left"
    assert c.reject_note == "moving too fast"


def test_good_enough():
    m = build_view_model([good()])
    assert m.banner_text == "GOOD ENOUGH" and m.banner_class == "green" and m.all_good
    c = m.cameras[0]
    assert c.could_change.startswith("could still change by: 2.5 px")
    assert "lens correction itself: 9.0 px" in c.could_change
    assert c.could_change_class == "green" and c.error_class == "green"
    assert "Press Enter" in m.next_line


def test_two_cameras_overall_banner_and_prefixed_hints():
    m = build_view_model([good("a"), keep("b")])
    assert m.banner_text == "KEEP GOING" and not m.all_good
    assert m.next_line == "CAM b: tilt the board left"


def test_errors_shown():
    s = IntrinsicsStatus(camera="a", errors=("solver exploded",))
    c = build_view_model([s]).cameras[0]
    assert c.error_now == "solve failed" and c.error_class == "red"
    assert "ERROR: solver exploded" in "\n".join(status_text_lines([s]))


def test_text_lines_carry_view_model_fields():
    for st in (show(), keep(), good()):
        m = build_view_model([st])
        text = "\n".join(status_text_lines([st]))
        c = m.cameras[0]
        for field in (m.banner_text, c.views_line, c.error_now, c.could_change):
            assert field in text
        if c.reject_note:
            assert c.reject_note in text
        if m.next_line:
            assert m.next_line in text
        assert "tilt     " + " ".join(f"{t.name}:{t.count or '.'}" for t in c.tilts) in text


def test_finish_text():
    ok = finish_text_lines(IntrinsicsFinishResult(installed=True, reason=None, status=good()))
    assert "INSTALLED" in ok[0] and "run aprilcam camera calibrate a to apply" in ok[1]
    no = finish_text_lines(
        IntrinsicsFinishResult(installed=False, reason="not good enough", status=keep()))
    assert "NOT installed" in no[0] and "not good enough" in no[1]
