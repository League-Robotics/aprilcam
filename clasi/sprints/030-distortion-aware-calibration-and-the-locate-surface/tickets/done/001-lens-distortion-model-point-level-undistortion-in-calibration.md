---
id: '001'
title: 'Lens distortion model: point-level undistortion in Calibration'
status: done
use-cases:
- SUC-001
depends-on: []
github-issue: ''
issue: v2-calibration-does-not-model-lens-distortion.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Lens distortion model: point-level undistortion in Calibration

## Description

v2's `Calibration.to_world`/`to_pixel` are a single projective transform
with no lens model, a measured regression from v1 (see the linked
issue: -0.96 correlation between apparent tag size and image radius^2,
1.4-2.2 cm of positional error once distortion is modelled). This
ticket lands the foundation: optional `camera_matrix`/`dist_coeffs`
storage on `Calibration`, point-level undistortion/redistortion inside
`to_world`/`to_pixel`, and the undistort-before-solve step inside
`Calibrator.calibrate()`. It does **not** add a way to *estimate*
intrinsics (that's ticket 002) — this ticket's own tests construct
`Calibration`/intrinsics values by hand to prove the math and the
storage round-trip.

See sprint.md's Architecture section (Design Rationale Decision 1) for
why this is point-level undistortion confined to
`calibration/geometry.py`/`calibration/calibration.py`, never a
whole-frame undistort — mined from v1's `stream.py:433` for the
*problem* and the *solve-order requirement*, deliberately not ported
for the *mechanism* (v2's `Deskewer` already builds its warp from four
points via `to_pixel`, not a full-frame remap).

## Acceptance Criteria

- [x] `calibration/geometry.py` gains `undistort_point(pixel, camera_matrix, dist_coeffs) -> PixelPoint`, a thin `cv2.undistortPoints(..., P=camera_matrix)` wrapper (reprojects back into pixel space, not normalized coordinates — matches every other point convention in this module).
- [x] `calibration/geometry.py` gains `distort_point(pixel, camera_matrix, dist_coeffs) -> PixelPoint`, the inverse, via a short fixed-point/Newton iteration seeded at the input point (OpenCV has no direct `distortPoints`); docstring states the convergence assumption and that it is only ever called on a handful of points per use (e.g. `Deskewer`'s 4 corners), never per-frame at scale.
- [x] `Calibration` (`calibration/calibration.py`) gains `camera_matrix: Optional[Tuple[_Row3, _Row3, _Row3]] = None` and `dist_coeffs: Optional[Tuple[float, ...]] = None`.
- [x] `to_dict()`/`from_dict()` round-trip both fields; both omitted from the JSON when `None` (mirrors the "never a null placeholder" rule `reprojection_error_cm` already follows); a `calibration.json` written before this ticket (no such keys present) loads successfully with both fields `None`.
- [x] `Calibration.to_world` undistorts the input pixel via `geometry.undistort_point` before applying the homography when `camera_matrix`/`dist_coeffs` are set; output is byte-for-byte identical to today's behaviour when they are `None`.
- [x] `Calibration.to_pixel` applies the inverse homography then redistorts via `geometry.distort_point` when set; identical to today when `None`.
- [x] `Calibrator.calibrate()`: immediately before `_solve_homography`/`_reprojection_error_cm`, undistort `pixel_pts` via `geometry.undistort_point` whenever intrinsics are known for the camera slug being calibrated (this ticket only needs the carry-forward-and-undistort mechanics — set up its own tests with hand-constructed prior `Calibration` instances that already carry intrinsics; ticket 002 is what lets an operator produce those intrinsics in the first place).
- [x] Unit test: `to_world(to_pixel(w))` round-trips within numerical tolerance for a `Calibration` with intrinsics set and a nontrivial homography.
- [x] Unit test: a homography solved from a synthetically-distorted marker-pixel set (known ground-truth distortion applied to synthetic points, then run through `Calibrator.calibrate()`'s solve path) matches the known-good homography solved directly from the undistorted points, once intrinsics are supplied.
- [x] Unit test: loading a pre-sprint `calibration.json` fixture (no `camera_matrix`/`dist_coeffs` keys) succeeds and both fields are `None`.
- [x] `git diff` for this ticket touches only `calibration/geometry.py`, `calibration/calibration.py`, and tests — no change to any `to_world`/`to_pixel` call site (`daemon/detection.py`, the drift check, `calibration/deskew.py`), proving the containment claim in sprint.md's Architecture section.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/calibration/ tests/aprilcam/daemon/test_detection.py -v` — confirm zero behavioural change for every existing (intrinsics-absent) case.
- **New tests to write**: `tests/aprilcam/calibration/test_geometry.py` (`undistort_point`/`distort_point` round-trip against hand-computed OpenCV values), `tests/aprilcam/calibration/test_calibration.py` (intrinsics storage round-trip, solve-order, backward-compat load of a pre-sprint fixture).
- **Verification command**: `uv run pytest tests/aprilcam/calibration/ -v`

## Completion Notes

**Implementation.** `calibration/geometry.py` gained `undistort_point`
(thin `cv2.undistortPoints(..., P=camera_matrix)` wrapper) and
`distort_point` (fixed-point iteration: `guess += target -
undistort_point(guess)`, seeded at the input point, deliberately
expressed in terms of `undistort_point` itself rather than a
hand-derived forward distortion polynomial, so it inverts whatever
`cv2.undistortPoints` actually computes for any `dist_coeffs` length).
`Calibration` gained `camera_matrix`/`dist_coeffs` (both
`Optional`, default `None`); `to_world`/`to_pixel` undistort/redistort
via those two functions only when both fields are set;
`to_dict`/`from_dict` omit the keys entirely when `None` (mirrors
`CameraConfig.to_dict`'s `camera_position` treatment). `Calibrator.
calibrate()` reads any prior `Calibration` already held in its own
in-process cache (`self.get_calibration(camera_slug)`), undistorts
`pixel_pts` before `_solve_homography`/`_reprojection_error_cm` when
that prior calibration carries intrinsics, and carries `camera_matrix`/
`dist_coeffs` forward unchanged onto the freshly solved `Calibration`
(Decision 3). `git diff` for this ticket touches only
`calibration/geometry.py`, `calibration/calibration.py`, and the two
matching test files — verified directly, no other file changed.

**Testing.** `uv run pytest tests/aprilcam/calibration/ -v` (97 tests)
and `tests/aprilcam/calibration/ tests/aprilcam/daemon/test_detection.py`
(156 tests) both green. New tests: `test_geometry.py` gained an
`undistort_point`/`distort_point` section (wrapper-matches-cv2 check,
zero-distortion identity, off-centre sign sanity check, parametrized
true-inverse round trips, and a dedicated regression test for a
strong/off-centre case, see below). `test_calibration.py` gained a
lens-distortion section: `to_dict`/`from_dict` round trip and
key-omission, a hand-built pre-sprint fixture (no `camera_matrix`/
`dist_coeffs` keys) loading with both `None`, `to_world`/`to_pixel`
isolation tests (identity homography, so each test observes only the
undistort or only the redistort step), the intrinsics-set round-trip
test, and the synthetic-distortion `Calibrator.calibrate()` test:
main-playfield's 10 ArUco markers rendered at positions produced by an
**independent** forward-distortion formula (not `geometry.
distort_point` itself, to avoid a circular test), run through a
`Calibrator` seeded with a hand-built prior `Calibration` carrying
matching intrinsics, and checked against a homography solved directly
from the undistorted positions — matching within 0.5 cm, with
`reprojection_error_cm` on the same undistorted point set staying
under 1.0 cm (the v1-earned "H and RMS must use the same undistorted
set" lesson, `archive/.../calibration.py:1342-1346`).

**A real bug found and fixed while writing the tests.** The strong
synthetic-distortion fixture (`k1 = -0.5`, a smaller focal length so an
off-centre point sits at normalized radius ~0.53) exposed that
`distort_point`'s original `_DISTORT_POINT_MAX_ITERATIONS = 20` cap was
too small: this case needs ~55 iterations to converge (the plain
`guess += residual` Picard update's convergence *rate* degrades as
distortion strengthens, even though it still converges). Fixed by
raising the cap to 200 (cost is negligible — a handful of extra
`cv2.undistortPoints` calls on a single point, never per-frame) and
correcting the docstring's over-specific "converges in 2-4 iterations"
claim to describe the actual rate-varies-with-distortion-strength
behaviour. A dedicated regression test
(`test_distort_point_converges_for_strong_off_centre_distortion`) pins
this case so a future change to the cap or update rule cannot silently
reintroduce it.

**Interface notes for ticket 002 and 003.**

1. **Carry-forward source is the `Calibrator`'s own in-process cache,
   not `DaemonService`'s.** `Calibrator.calibrate()` looks up prior
   intrinsics via `self.get_calibration(camera_slug)` — its own
   internal `self._calibrations` dict, populated only by a prior
   successful `calibrate()` call *on this same `Calibrator` instance*.
   `daemon/service.py`'s `_load_calibrations()` (daemon startup, reload
   from `calibration.json`) populates `DaemonService._calibrations`
   directly and **never** seeds `self._calibrator._calibrations` — the
   two dicts are entirely separate stores. Net effect: right now, a
   camera recalibrated (no `--with-intrinsics`) in the *same* daemon
   process that estimated its intrinsics carries them forward
   correctly; a camera recalibrated *after a daemon restart* would not
   — the freshly-constructed `Calibrator` has no memory of the
   `camera_matrix`/`dist_coeffs` that `calibration.json` still holds on
   disk, so a post-restart no-flag recalibration would silently wipe
   the stored intrinsics from the persisted file. This ticket's own
   diff-containment acceptance criterion forbids touching
   `daemon/service.py`, so this is left for ticket 002 (which already
   touches `daemon/service.py`/`Calibrator.calibrate()`'s
   `estimate_intrinsics` plumbing) to close — likely by seeding
   `Calibrator`'s cache from `DaemonService._calibrations` after
   `_load_calibrations()`, or by threading the prior `Calibration`
   through some other injected lookup. Ticket 002's own acceptance
   criterion ("if a `Calibration` already exists for this camera slug
   with intrinsics set, the new `Calibration` this call constructs
   carries `camera_matrix`/`dist_coeffs` forward unchanged") only truly
   holds end-to-end once this is addressed.
2. **`_has_intrinsics()` requires both fields set, not either.** A
   `Calibration` with only one of `camera_matrix`/`dist_coeffs` set
   (should never happen from any producer in this codebase, but is
   possible from a hand-constructed or corrupted record) is treated as
   "no lens model" rather than raising — worth keeping in mind if
   ticket 002's estimation path ever needs to construct one field ahead
   of the other.
3. **`dist_coeffs` round-trips as whatever length is given** — `from_dict`
   does not validate length against 4/5/8/12/14 (OpenCV's own accepted
   shapes); `cv2.undistortPoints`/`cv2.calibrateCamera` will raise on an
   invalid length at call time. Ticket 002's estimation path should
   decide whether to validate the length explicitly at store time
   rather than relying on that late failure.
