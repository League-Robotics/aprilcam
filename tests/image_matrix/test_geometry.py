"""L0 geometry-convention assertions (sprint 035 ticket 006; test plan
§6 item 3): the axis-mirror regression check.

Using real detections against real fixture images -- never a synthetic
homography -- this asserts the world-coordinates-first convention
(``design.md`` Sec.6, cited by this ticket's own acceptance criterion as
"design.md §2 rule 5a"): **+x is east, +y is north, and a deskewed image
is north-up** (image rows grow downward, so a point further north sits
at a *smaller* pixel-y -- the single sanctioned y-flip,
``calibration.geometry.world_to_deskewed_pixel``'s own docstring).

**One image per camera, not every fixture.** This ticket's acceptance
criterion asks for the convention checked "for at least one
confirmed-fixture image per camera" -- unlike :mod:`test_recall`/
:mod:`test_world_accuracy`/:mod:`test_calibration_robustness`, which
parametrize over *every* discovered image, this module defines its own
``pytest_generate_tests`` (a test module implementing this hook
alongside :mod:`tests.image_matrix.conftest`'s own is pytest-supported;
both fire, each parametrizing a *different*-named argument) that reuses
:func:`~tests.image_matrix.conftest.discover_fixture_entries` --
ticket 005's own discovery machinery, unchanged -- and reduces its
result to one representative :class:`~tests.image_matrix.conftest.FixtureEntry`
per ``camera_slug`` (preferring the ``"baseline"`` config, where every
camera's real, near-baseline recall is at its best -- see
:mod:`test_recall`'s earned data). This is a convention check, not a
recall sweep: repeating it over every degraded-exposure image would add
no new signal (a config that dropped the very reference markers this
check needs would just skip, per the guard below), while a *wrong* sign
convention would show up identically on every single image, baseline
included.

**Pipeline: the same two calls every other L0 module makes, chained
with the already-approved calibration/geometry primitives.** For each
picked fixture: ``vision.pipeline.run_pipeline`` ->
``aprilcam.daemon.detection._default_detector_factory(slug).detect``
(no second detection code path -- identical to
:mod:`test_recall`/:mod:`test_world_accuracy`/
:mod:`test_calibration_robustness`), then each detected ArUco's centre
through the fixture's own pinned, sha256-verified calibration
(``Calibration.to_world``, the same call :mod:`test_world_accuracy`
makes) and finally through ``calibration.geometry.world_to_deskewed_pixel``
-- the exact pure function ``calibration/deskew.py``'s own docstring
says is the single source of truth both the real warp
(``Deskewer.deskew``) and ``client/renderer.py`` share. This checks the
real, end-to-end pipeline's sign convention on genuinely-detected real
pixels, not a hand-built matrix (``tests/aprilcam/calibration/
test_geometry.py`` already covers that half, synthetically, and is
cited here rather than re-derived, per this ticket's own implementation
plan).

**Reference pairs are survey facts, read once and hardcoded per
playfield -- never re-derived per test run.** ``main-playfield.json``:
ArUco 4 (east internal, x=+67) vs ArUco 8 (west internal, x=-67), same
y=0; ArUco 2 (north corner, x=0, y=+44.65) vs ArUco 6 (south corner,
x=0, y=-44.65), same x=0. ``secondary-playfield.json`` (camera 2's
field, only 4 ArUco markers, no centre AprilTag): ArUco 1 (northeast,
x=+54.25) vs ArUco 4 (northwest, x=-54.25), same y=+35.4; ArUco 1
(northeast, y=+35.4) vs ArUco 2 (southeast, y=-35.4), same x=+54.25.
None of these ids is ArUco 1 *on camera 5* (the one marker documented
intermittent there, sprint 035's own measured 7/12) -- main-playfield's
pairs use ids 2/4/6/8 exclusively, so this check never depends on the
one marker known not to be reliably present.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, Optional

import cv2 as cv
import pytest

from aprilcam.calibration import geometry
from aprilcam.calibration.calibration import Calibration
from aprilcam.config.loader import ConfigLoader
from aprilcam.daemon.detection import _default_detector_factory
from aprilcam.types import PixelPoint
from aprilcam.vision.pipeline import run_pipeline

from tests.image_matrix.conftest import PLAYFIELDS_DIR, FixtureEntry, discover_fixture_entries

__all__ = ["test_geometry_east_west_and_north_up"]

_loader = ConfigLoader()


@dataclass(frozen=True, kw_only=True)
class _AxisPair:
    """Two surveyed ArUco ids on the same playfield that differ mainly
    along one world axis, sharing (near enough) the other -- the
    minimal fact needed to catch a sign flip on that axis."""

    east_id: Optional[int] = None
    west_id: Optional[int] = None
    north_id: Optional[int] = None
    south_id: Optional[int] = None


#: East/west reference pair per playfield (module docstring's survey facts).
_EAST_WEST_PAIRS: Dict[str, _AxisPair] = {
    "main-playfield": _AxisPair(east_id=4, west_id=8),
    "secondary-playfield": _AxisPair(east_id=1, west_id=4),
}

#: North/south reference pair per playfield (module docstring's survey facts).
_NORTH_SOUTH_PAIRS: Dict[str, _AxisPair] = {
    "main-playfield": _AxisPair(north_id=2, south_id=6),
    "secondary-playfield": _AxisPair(north_id=1, south_id=2),
}


def pytest_generate_tests(metafunc: "pytest.Metafunc") -> None:
    """Parametrize ``geometry_fixture_entry`` over one representative
    :class:`FixtureEntry` per ``camera_slug`` -- module docstring's "one
    image per camera" section. Does nothing for any other test function
    (in particular, never touches ``fixture_entry`` -- that stays
    :mod:`tests.image_matrix.conftest`'s own hook's job)."""
    if "geometry_fixture_entry" not in metafunc.fixturenames:
        return

    result = discover_fixture_entries()
    if not result.entries:
        metafunc.parametrize(
            "geometry_fixture_entry",
            [pytest.param(None, marks=pytest.mark.skip(reason=result.skip_reason), id="no-fixtures")],
        )
        return

    ordered_entries = sorted(result.entries, key=lambda e: e.fixture_id)
    chosen: Dict[str, FixtureEntry] = {}
    for entry in ordered_entries:
        if entry.config_label == "baseline" and entry.camera_slug not in chosen:
            chosen[entry.camera_slug] = entry
    for entry in ordered_entries:
        chosen.setdefault(entry.camera_slug, entry)

    picked = [chosen[slug] for slug in sorted(chosen)]
    metafunc.parametrize("geometry_fixture_entry", picked, ids=[e.fixture_id for e in picked])


def _deskewed_aruco_positions(entry: FixtureEntry) -> Dict[int, PixelPoint]:
    """Detected ArUco id -> its position on the deskewed image plane for
    *entry*, via the real detect -> to_world -> world_to_deskewed_pixel
    chain (module docstring)."""
    assert entry.calibration_path is not None  # caller's guard, see test body

    calibration_data = json.loads(entry.calibration_path.read_text())
    calibration = Calibration.from_dict(calibration_data)

    definition = _loader.load_playfield(PLAYFIELDS_DIR / f"{entry.playfield}.json")
    width_cm, height_cm = definition.width_cm, definition.height_cm
    output_width_px, _output_height_px = geometry.deskewed_output_size(width_cm, height_cm)
    px_per_cm = output_width_px / width_cm

    frame = cv.imread(str(entry.image_path))
    assert frame is not None, f"{entry.fixture_id}: failed to read {entry.image_path}"
    gray, rescale = run_pipeline(frame, None)
    detector = _default_detector_factory(entry.camera_slug)
    detections = detector.detect(gray, scale=rescale.scale)

    positions: Dict[int, PixelPoint] = {}
    for detection in detections:
        if detection.family.value != "aruco":
            continue
        world = calibration.to_world(detection.center)
        positions[detection.number] = geometry.world_to_deskewed_pixel(
            world, world_extent=(width_cm, height_cm), px_per_cm=px_per_cm
        )
    return positions


def test_geometry_east_west_and_north_up(geometry_fixture_entry: FixtureEntry) -> None:
    entry = geometry_fixture_entry

    if entry.calibration_path is None:
        pytest.skip(
            f"{entry.fixture_id}: no pinned, sha256-verified calibration snapshot -- "
            "the geometry-convention check needs a calibrated camera to reach world/"
            "deskewed-pixel space at all"
        )

    east_west = _EAST_WEST_PAIRS.get(entry.playfield)
    north_south = _NORTH_SOUTH_PAIRS.get(entry.playfield)
    assert east_west is not None and north_south is not None, (
        f"{entry.fixture_id}: no east/west or north/south reference pair is defined for "
        f"playfield {entry.playfield!r} in test_geometry.py -- add one before trusting this "
        "camera's geometry convention"
    )

    positions = _deskewed_aruco_positions(entry)

    needed_ids = {
        east_west.east_id,
        east_west.west_id,
        north_south.north_id,
        north_south.south_id,
    }
    missing = needed_ids - positions.keys()
    assert not missing, (
        f"{entry.fixture_id}: reference ArUco id(s) {sorted(missing)} were not detected -- "
        f"cannot check the east/north convention without them (detected ArUco ids: "
        f"{sorted(positions)})"
    )

    east_px = positions[east_west.east_id]
    west_px = positions[east_west.west_id]
    assert east_px.x > west_px.x, (
        f"{entry.fixture_id}: east marker (ArUco {east_west.east_id}, deskewed x="
        f"{east_px.x:.1f}px) is not further right than the west marker (ArUco "
        f"{east_west.west_id}, deskewed x={west_px.x:.1f}px) -- the +x-east convention "
        "(design.md Sec.6) is broken, or the deskewed image is mirrored left/right"
    )

    north_px = positions[north_south.north_id]
    south_px = positions[north_south.south_id]
    assert north_px.y < south_px.y, (
        f"{entry.fixture_id}: north marker (ArUco {north_south.north_id}, deskewed y="
        f"{north_px.y:.1f}px) is not higher (smaller pixel-y) than the south marker "
        f"(ArUco {north_south.south_id}, deskewed y={south_px.y:.1f}px) -- the north-up "
        "convention (design.md Sec.6) is broken, or the deskewed image is flipped top/bottom"
    )
