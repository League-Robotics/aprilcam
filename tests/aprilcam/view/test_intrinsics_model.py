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


# --- sprint 046-002: overlay geometry --------------------------------------
import pytest

from aprilcam.view import intrinsics_model as im


@pytest.mark.parametrize("size", [(1920, 1080), (101, 77), (640, 480), (1281, 721), (50, 200)])
def test_grid_rects_tile_exactly(size):
    w, h = size
    r = im.grid_rects(w, h)
    assert len(r) == 9
    assert sum((x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in r) == w * h
    assert r[0][:2] == (0, 0) and r[8][2:] == (w, h)
    # columns / rows share boundaries (NW..SE row-major)
    assert r[0][2] == r[1][0] == r[3][2] == r[4][0]
    assert r[2][0] == r[1][2] and r[6][1] == r[3][3]
    assert abs((r[0][2] - r[0][0]) - w / 3) <= 1
    assert abs((r[1][2] - r[1][0]) - w / 3) <= 1
    assert abs((r[0][3] - r[0][1]) - h / 3) <= 1


def test_grid_parity_with_collector():
    from aprilcam.intrinsics import collector
    assert im.GRID_EDGE_FRACTION == collector.EDGE_FRACTION
    for w, h in [(1920, 1080), (101, 77), (640, 480), (1281, 721)]:
        for i, (x0, y0, x1, y1) in enumerate(im.grid_rects(w, h)):
            assert im.CELL_ORDER[i] == collector.REGION_NAMES[i]
            assert collector._region_index((x0 + x1) / 2, (y0 + y1) / 2, w, h) == i


def test_cell_tint_matches_tile_level():
    st = keep()
    cm = im.build_camera_model(st)
    ov = im.cell_overlays(cm.cells)
    assert [o.name for o in ov] == list(im.CELL_ORDER)
    for tile, o in zip(cm.cells, ov):
        assert o.level == tile.level
        assert (o.tint == im.tint_colour("good")) == (tile.level == "good")
        assert (o.tint is None) == (tile.level == "none")
        assert (o.tint == im.tint_colour("some")) == (tile.level == "some")
    assert {o.level for o in ov} == {"none", "some", "good"}
    assert im.tint_colour("some") != im.tint_colour("good")
    assert ov[0].label == "NW 2"


def test_dim_colour():
    assert im.dim_colour("#ffffff", 0.5) == "#7f7f7f"
    assert im.dim_colour("#102030", 0.0) == "#102030"
    assert im.dim_colour("#ffffff") != "#ffffff"


def test_fit_rect_letterbox_and_scale():
    # wide image in a tall column: width-limited, centred vertically
    assert im.fit_rect((200, 100), (0, 0, 100, 100)) == (0, 25, 100, 75)
    # tall image in a wide column: height-limited, centred horizontally, offset column
    assert im.fit_rect((100, 200), (10, 20, 210, 120)) == (85, 20, 135, 120)
    # scale up
    assert im.fit_rect((10, 10), (0, 0, 100, 50)) == (25, 0, 75, 50)


@pytest.mark.parametrize("size", [None, (0, 0), (-1, 5), (5, 0)])
def test_fit_rect_degenerate(size):
    assert im.fit_rect(size, (3, 4, 100, 100)) == (3, 4, 4, 5)
    assert im.fit_rect((10, 10), (0, 0, 0, 50)) == (0, 0, 1, 1)


def test_scale_polygons():
    poly = (((0, 0), (100, 0), (100, 50), (0, 50)),)
    assert im.scale_polygons(poly, (100, 50), (10, 20, 210, 120)) == (
        ((10.0, 20.0), (210.0, 20.0), (210.0, 120.0), (10.0, 120.0)),)
    # scale down
    out = im.scale_polygons(poly, (100, 50), (0, 0, 50, 25))
    assert out[0][2] == (50.0, 25.0)
    assert im.scale_polygons(None, (100, 50), (0, 0, 10, 10)) == ()
    assert im.scale_polygons((), (100, 50), (0, 0, 10, 10)) == ()
    assert im.scale_polygons(poly, (0, 0), (0, 0, 10, 10)) == ()
    assert im.scale_polygons(poly, None, (0, 0, 10, 10)) == ()
