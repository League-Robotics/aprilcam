"""Tests for ``aprilcam.vision.detector`` (headless, no hardware).

Covers ticket 021-003's acceptance criteria: id+family recovery, family
separation, the ``-(N+1)``-encoding-never-happens guarantee, corner
subpixel stability/determinism, the ``scale`` rescale contract, and --
the ticket's one genuinely open empirical question -- verification that
the detector's native corner order is stable relative to the tag's own
printed pattern (not the image frame) under a known physical rotation,
which is what makes the module docstring's "corner 0 = front-left"
convention checkable at all.

Two fixture sources, per the ticket's testing plan:

* **Real captured playfield images** (``tests/images/``, restored by
  ticket 001) -- smoke-tested only (runs without error, plausible
  structure). ``tests/images/field-state.json`` is stakeholder-unconfirmed
  (sprint.md Open Question 1), so no specific tag-id set from these
  images is asserted here.
* **Synthetic tag images** (``_tag_fixtures.py``, this ticket's own
  helper -- v2's CLI has no ``taggen`` subcommand yet) -- used for every
  test that needs *known* ground truth: id, family, rotation.
"""

from __future__ import annotations

from pathlib import Path

import cv2 as cv
import numpy as np
import pytest

from aprilcam.types import TagFamily
from aprilcam.vision.detector import Detection, DetectorConfig, TagDetector

from ._tag_fixtures import synthetic_tag_image

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_DIR = REPO_ROOT / "tests" / "images" / "arducam-ov9782-usb-camera"
BASELINE_JPG = FIXTURE_DIR / "baseline.jpg"


def _load_gray_fixture() -> np.ndarray:
    """The arducam baseline fixture, decoded straight to grayscale.

    Skips cleanly (sprint 035 ticket 008's gate-invariance fix) when
    ``tests/images/`` is absent or renamed, rather than failing --
    matching ``tests/image_matrix/conftest.py``'s "skipped, with a
    reason" convention for a rotted/missing fixture corpus.
    """
    if not BASELINE_JPG.is_file():
        pytest.skip(f"fixture missing: {BASELINE_JPG} (tests/images/ absent or renamed)")
    image = cv.imread(str(BASELINE_JPG), cv.IMREAD_GRAYSCALE)
    if image is None:
        pytest.skip(f"fixture unreadable: {BASELINE_JPG}")
    return image


def _apply_affine(points: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Apply a 2x3 affine matrix (as produced by ``cv2.getRotationMatrix2D``) to *points*.

    Plain numpy rather than ``cv2.warpAffine``/``cv2.transform``, so this
    reference calculation is independent of any cv2 shape/dtype quirks
    the code under test might also be subject to.
    """
    ones = np.ones((points.shape[0], 1), dtype=np.float64)
    homogeneous = np.hstack([points.astype(np.float64), ones])
    return (matrix @ homogeneous.T).T


# ---------------------------------------------------------------------------
# Smoke tests -- real captured fixtures
# ---------------------------------------------------------------------------


def test_smoke_detect_runs_without_error_on_real_fixture():
    gray = _load_gray_fixture()
    hits = TagDetector().detect(gray)
    assert isinstance(hits, list)
    for hit in hits:
        assert isinstance(hit, Detection)
        assert hit.family in (TagFamily.APRILTAG, TagFamily.ARUCO)
        assert hit.number >= 0  # never a v1-style -(N+1) encoding
        assert hit.corners.shape == (4, 2)
        assert hit.corners.dtype == np.float32


def test_real_fixture_detection_is_deterministic():
    gray = _load_gray_fixture()
    detector = TagDetector()
    first = sorted(detector.detect(gray), key=lambda h: (h.family, h.number))
    second = sorted(detector.detect(gray), key=lambda h: (h.family, h.number))
    assert [(h.family, h.number) for h in first] == [(h.family, h.number) for h in second]
    for a, b in zip(first, second):
        np.testing.assert_array_equal(a.corners, b.corners)


# ---------------------------------------------------------------------------
# Ground-truth recall -- synthetic fixtures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "family, tag_id", [(TagFamily.APRILTAG, 7), (TagFamily.ARUCO, 3), (TagFamily.ARUCO, 0)]
)
def test_synthetic_tag_recovers_correct_id_and_family(family, tag_id):
    image = synthetic_tag_image(family, tag_id)
    hits = TagDetector().detect(image)
    assert len(hits) == 1
    assert hits[0].family == family
    assert hits[0].number == tag_id
    assert hits[0].corners.shape == (4, 2)


def test_single_family_image_reports_no_cross_family_hits():
    """An ArUco-only image reports no AprilTag hits, and vice versa.

    Deliberately uses the *same* numeric id (4) for both images: if the
    two families' dictionaries were ever conflated internally, reusing an
    id makes that show up as a real cross-family false positive rather
    than being masked by non-overlapping id ranges.
    """
    detector = TagDetector()

    apriltag_only = synthetic_tag_image(TagFamily.APRILTAG, 4)
    apriltag_hits = detector.detect(apriltag_only)
    assert len(apriltag_hits) >= 1
    assert all(h.family == TagFamily.APRILTAG for h in apriltag_hits)

    aruco_only = synthetic_tag_image(TagFamily.ARUCO, 4)
    aruco_hits = detector.detect(aruco_only)
    assert len(aruco_hits) >= 1
    assert all(h.family == TagFamily.ARUCO for h in aruco_hits)


def test_repeated_detection_on_identical_image_yields_identical_corners():
    image = synthetic_tag_image(TagFamily.APRILTAG, 9, rotation_deg=15.0)
    detector = TagDetector()
    first = detector.detect(image)
    second = detector.detect(image)
    assert len(first) == len(second) == 1
    assert first[0].family == second[0].family
    assert first[0].number == second[0].number
    np.testing.assert_array_equal(first[0].corners, second[0].corners)


def test_corner_subpixel_positions_stable_across_repeated_runs():
    """No randomness in the pipeline or detector configuration (acceptance criterion)."""
    image = synthetic_tag_image(TagFamily.ARUCO, 12, rotation_deg=22.5)
    detector = TagDetector()
    runs = [detector.detect(image) for _ in range(8)]
    assert all(len(hits) == 1 for hits in runs)
    reference = runs[0][0].corners
    epsilon = 1e-4
    for hits in runs[1:]:
        np.testing.assert_allclose(hits[0].corners, reference, atol=epsilon)


# ---------------------------------------------------------------------------
# Corner order / boundary-hat convention -- the ticket's open question
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("family", [TagFamily.APRILTAG, TagFamily.ARUCO])
def test_canonical_orientation_front_edge_is_corners_0_to_1(family):
    """At the marker's canonical (unrotated) print orientation, corners[0]->corners[1]
    is the top edge -- the edge this module's docstring designates "front" by
    convention. Confirms the documented boundary/hat mapping concretely:
    corners[0]/[1] (top, left-to-right) = front-left/front-right,
    corners[2]/[3] (bottom, right-to-left) = back-right/back-left.
    """
    image = synthetic_tag_image(family, 5, rotation_deg=0.0)
    hits = TagDetector().detect(image)
    assert len(hits) == 1
    c = hits[0].corners

    # Top edge (corners 0, 1) and bottom edge (corners 2, 3) are each
    # horizontal, and the top edge is above (smaller y than) the bottom.
    assert c[0][1] == pytest.approx(c[1][1], abs=2.0)
    assert c[2][1] == pytest.approx(c[3][1], abs=2.0)
    assert c[0][1] < c[2][1]

    # Left-to-right ordering within each edge: 0 left of 1 (front-left,
    # front-right); 3 left of 2 (back-left, back-right).
    assert c[0][0] < c[1][0]
    assert c[3][0] < c[2][0]


@pytest.mark.parametrize("family", [TagFamily.APRILTAG, TagFamily.ARUCO])
@pytest.mark.parametrize("angle_deg", [90.0, 180.0, 270.0, 37.0])
def test_corner_order_tracks_the_pattern_not_the_image_frame(family, angle_deg):
    """The empirical check behind the module docstring's central claim.

    Detected corner index *k* is always the same physical corner of the
    printed marker, regardless of how the marker is physically rotated in
    the image -- verified by rotating a synthetic tag by a known angle and
    checking that each detected corner lands where the *unrotated*
    detection's corresponding corner, rotated by that same known
    transform, predicts it should. If the detector instead returned
    corners in image-relative order (e.g. always "topmost first"), this
    would fail for every non-trivial rotation.
    """
    canvas_size = 600
    tag_id = 6

    reference_image = synthetic_tag_image(family, tag_id, rotation_deg=0.0, canvas_size=canvas_size)
    reference_hits = TagDetector().detect(reference_image)
    assert len(reference_hits) == 1
    reference_corners = reference_hits[0].corners

    rotated_image = synthetic_tag_image(
        family, tag_id, rotation_deg=angle_deg, canvas_size=canvas_size
    )
    rotated_hits = TagDetector().detect(rotated_image)
    assert len(rotated_hits) == 1

    center = (canvas_size / 2, canvas_size / 2)
    matrix = cv.getRotationMatrix2D(center, angle_deg, 1.0)
    expected_corners = _apply_affine(reference_corners, matrix)

    # Generous tolerance: warpAffine interpolation and subpixel corner
    # refinement both introduce a few pixels of slop; what this test
    # guards against is corner *index reassignment* (tens to hundreds of
    # pixels off, or a systematic 90-degree-equivalent permutation), not
    # sub-pixel drift.
    np.testing.assert_allclose(rotated_hits[0].corners, expected_corners, atol=6.0)


# ---------------------------------------------------------------------------
# No -(N+1) encoding, ever
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tag_id", [0, 1, 4, 8, 15, 30])
@pytest.mark.parametrize("family", [TagFamily.APRILTAG, TagFamily.ARUCO])
def test_no_negative_ids_ever_returned(family, tag_id):
    image = synthetic_tag_image(family, tag_id)
    hits = TagDetector().detect(image)
    assert len(hits) == 1
    assert hits[0].number == tag_id
    assert hits[0].number >= 0


# ---------------------------------------------------------------------------
# scale contract (pipeline.Rescale interop, without importing pipeline.py)
# ---------------------------------------------------------------------------


def test_scale_parameter_maps_corners_back_to_original_pixel_space():
    full_res = synthetic_tag_image(TagFamily.APRILTAG, 6, canvas_size=600)
    half_res = cv.resize(full_res, (300, 300), interpolation=cv.INTER_AREA)

    detector = TagDetector()
    full_res_hits = detector.detect(full_res, scale=1.0)
    half_res_hits = detector.detect(half_res, scale=0.5)

    assert len(full_res_hits) == len(half_res_hits) == 1
    # Rescaled corners from the half-resolution image should land close to
    # the full-resolution corners -- not exact (the resize is lossy), but
    # within a small pixel tolerance, not off by anything like the 2x
    # factor a missed rescale would produce.
    np.testing.assert_allclose(
        half_res_hits[0].corners, full_res_hits[0].corners, atol=5.0
    )


# ---------------------------------------------------------------------------
# DetectorConfig
# ---------------------------------------------------------------------------


def test_detector_accepts_custom_config():
    config = DetectorConfig(quad_decimate=2.0, corner_refine="none")
    detector = TagDetector(config)
    image = synthetic_tag_image(TagFamily.APRILTAG, 2)
    hits = detector.detect(image)
    assert len(hits) == 1
