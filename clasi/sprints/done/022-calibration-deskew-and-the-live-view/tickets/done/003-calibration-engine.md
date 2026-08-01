---
id: '003'
title: Calibration engine
status: done
use-cases:
- SUC-003
depends-on:
- '001'
github-issue: ''
issue: calibration-deskew-and-the-live-view.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Calibration engine

## Description

Implements SUC-003 (parent UC-04, UC-15): the `Calibrator`/
`Calibration`/`CalibrationResult` implementations, `calibration.json`
persistence/reload, the `Calibrate`/`GetCalibration` RPCs, and
`aprilcam camera calibrate <camera>`. This is the sprint's geometric
core — every sign and axis here is guilty until proven innocent per
`docs/design/design.md` §6 / `specification.md` §7 (A1-centred origin
via pure subtraction, never a y-flip; heading/yaw `atan2`, 0=east
CCW positive).

**Known field-coverage risk to confront in this ticket, not discover
mid-run**: camera 3 (`arducam-ov9782-usb-camera`) currently sees ArUco
1, 2, 4, 5, 7, 10 but **not** 3, 6, 8, 9 on the physical
`main-playfield`. Calibration is all-or-nothing against
`PlayfieldDefinition.required_aruco_ids()` — `main-playfield`'s
required set is all 10 (perimeter 1–8 + interior 9, 10). As currently
aimed, camera 3 **cannot calibrate**. Before ticket 007 (hardware
verification) can run, this ticket must either (a) confirm the camera
needs repositioning (use ticket 002's `camera view` to re-aim and
re-check coverage) or (b) confirm the required-id set itself needs
revisiting (e.g., is interior marker 9/10 physically mounted and just
out of the current field of view, or genuinely absent from the built
field?). Do not assume either answer — check against the physical
field and record which it was in this ticket's implementation notes.
This does not block this ticket's headless work, but it blocks ticket
007 and must not be left for that ticket to discover cold.

Builds:

- `calibration/calibration.py` (new) — `Calibrator.calibrate(
  camera_slug)`: multi-frame averaged ArUco corner detection (via the
  existing `vision/detector.py` from sprint 021, not reimplemented),
  id matching against the linked `PlayfieldDefinition`'s surveyed
  positions, all-or-nothing acceptance against `required_aruco_ids()`,
  homography solve, degenerate-geometry rejection (near-collinear
  markers, singular homography), `CalibrationResult` construction
  (`matched_ids`, `missing_ids`, `reprojection_error_cm` — computed
  and reported, no threshold enforced, per sprint.md Open Question 1).
  `NoPlayfieldLinked` raised when the camera has no playfield link
  (precondition failure, UC-15.S-2 — distinct from a `CalibrationResult`
  failure).
- `calibration/geometry.py` (new) — pure pixel↔world math backing
  `Calibration.to_world`/`to_pixel` (the deskew warp itself is ticket
  005's concern; this ticket only needs the point-conversion half).
- `daemon/service.py` — `calibrate`/`get_calibration` implemented;
  `calibration.json` written under the camera's state directory on
  success, loaded at daemon startup.
- `errors.py` — `NoPlayfieldLinked(AprilCamError)`, new.
- `cli/calibrate.py` (new) — `aprilcam camera calibrate <camera>`, pure
  RPC orchestrator (no vision work client-side), reports matched/
  missing ids, refuses `--all`.

**Archive pointers**: mine `archive/src/aprilcam/calibration/
calibration.py`'s `calibrate_from_playfield_def`,
`_assign_corners_by_position`, `calibrate_from_corners`, and
`_reprojection_rms` for the id-matching and homography-solve approach
and the hard-won sign conventions — this file is 1805 lines of v1-era
code; take the matching/solving *approach*, not the surrounding
persistence/CLI/robot-specific code around it (rebuilt fresh against
v2's interfaces). Mine `archive/src/aprilcam/calibration/geometry.py`'s
`_world_corners`/`corner_pixels_from_homography` for the
point-conversion math. Do **not** port `archive/src/aprilcam/cli/
calibrate_cli.py` or any `homocal`-shaped upper-left-origin corner
table — `specification.md` §6 explicitly condemns that as a foot-gun.

## Acceptance Criteria

- [x] **Geometry convention suite (permanent, never relaxed)**: from
      synthetic/fixture ArUco layouts with known east/north
      relationships — a marker surveyed east of another produces a
      **larger** `to_world(...).x`; a marker surveyed north of another
      produces a **larger** `to_world(...).y`; the origin shift is
      verified as pure subtraction, never a sign inversion on either
      axis.
- [x] All 10 required ArUco ids (perimeter 1–8, interior 9 and 10)
      present in a fixture detection set → `CalibrationResult.
      succeeded=True`, `matched_ids` contains all 10, `missing_ids`
      empty (UC-04.S-1).
- [x] One id absent from the fixture detection set (e.g., ArUco 6) →
      `succeeded=False`, `missing_ids == (6,)` exactly — never a count,
      never "9 of 10" (UC-04.S-2).
- [x] A failed calibration attempt does not overwrite or clear an
      existing good `calibration.json` — a prior-good calibration is
      byte-identical after a failed retry.
- [x] Calling `calibrate` on a camera with no playfield link raises
      `NoPlayfieldLinked` before any frame is captured — assert zero
      calls to the camera backend (UC-15.S-2).
- [x] Degenerate/near-collinear marker geometry (synthetic fixture) is
      rejected with a `CalibrationResult.reason` naming the problem,
      not silently accepted as a valid-but-bad homography.
- [x] `aprilcam camera calibrate --all` is refused, explaining
      calibration targets exactly one daemon (UC-15.S-4).
- [x] `Calibration.calibrated_at`/`playfield` provenance is persisted
      and reloaded identically after a simulated daemon restart (fresh
      `DaemonService` reading the same `calibration.json`).
- [x] Implementation notes in this ticket record the outcome of the
      camera-3 field-coverage check described above (repositioned
      camera vs. required-set understanding) — a blocking prerequisite
      for ticket 007, not optional documentation.

## Implementation Notes

**Approach.** `calibration/calibration.py`'s `Calibrator` never opens a
camera itself — it is constructed with `get_camera_config`/
`get_playfield`/`capture_frames` callables (dependency injection, per
the sprint architecture's "camera frame access is handed in" boundary),
so the `NoPlayfieldLinked` check (against `get_camera_config`) runs and
can raise *before* `capture_frames` — the daemon's one gateway to a
real backend — is ever called. `daemon/service.py` wires real
callables (`_camera_config_for_calibration`, `_playfield_for_
calibration`, `_capture_calibration_frames`) closing over its own
locked `_camera_configs`/`_playfields`/`_camera_for`. This is also what
made the "zero calls to the camera backend" acceptance criterion
provable with an exploding `backend_factory` test double, not just
documented.

The homography solve goes **directly from detected pixel positions to
a playfield definition's real, already A1-centred surveyed world
coordinates** (`PlayfieldDefinition.aruco_position`) via
`cv2.findHomography` — there is no intermediate corner-relative
coordinate system, no manual origin subtraction, and no manual y-flip
anywhere in `calibration.py` or `geometry.py`. This is a deliberate
structural choice, not an accident: it is what makes the "origin shift
is a pure subtraction, never a y-flip" invariant impossible to violate
by construction, rather than merely easy to get right. Degenerate
geometry (collinear correspondences) is rejected *before* calling into
OpenCV via an SVD-based collinearity check on both the world and pixel
point sets, since `cv2.findHomography` does not reliably refuse a
collinear input — it can return a numerically well-formed but
geometrically meaningless matrix, exactly the "valid-but-bad
homography" UC-04 warns against.

`Calibrate`/`GetCalibration` RPCs, `client/models.py`/
`client/daemon_client.py` wiring, and `cli/calibrate.py` were added in
this ticket too (the ticket description names them explicitly, even
though the Builds list only bullets `daemon/service.py`) — without
them `cli/calibrate.py`'s "pure RPC orchestrator" contract would have
nothing to orchestrate against. `cli/calibrate.py` lives in its own
file but is wired as `camera calibrate` (a sub-subcommand of
`cli/camera.py`'s existing `camera` parser tree), matching the
existing command-surface convention and the forward pointer
`cmd_view`'s docstring already left for this ticket.

**Verification method for the sign conventions.** Three independent
layers, from most to least isolated:

1. `tests/aprilcam/calibration/test_geometry.py` — pure math against
   hand-built homography matrices, including a deliberately
   *non-standard* mount (pixel-y increasing *with* world-y) to prove
   `pixel_to_world`/`apply_homography` track whatever the matrix
   encodes rather than a hardcoded flip that would only coincidentally
   pass for a "normal" mount.
2. `tests/aprilcam/calibration/test_calibration.py` — the same
   non-standard-mount check repeated through the *real* detect +
   `cv2.findHomography` solve pipeline against synthetic frames with
   real rendered ArUco 4x4 markers (`cv2.aruco.generateImageMarker`) at
   pixel positions computed from `main-playfield.json`'s actual
   surveyed cm coordinates, plus a direct "AprilTag 1 (held out of the
   ArUco correspondence set) reports world (0, 0)" interpolation check.
3. **Real hardware**, against the stakeholder's live daemon and the
   physically-mounted `main-playfield` (camera 3,
   `arducam-ov9782-usb-camera`) — see below. This is not a committed
   test (ticket 007 owns that), but a manual verification run during
   implementation, using the unmodified production CLI → RPC → daemon →
   `Calibrator` path:
   - `aprilcam camera calibrate arducam-ov9782-usb-camera` → **succeeded**,
     all 10 ArUco ids matched, reprojection error **0.187 cm**.
   - AprilTag 1's real detected pixel position, converted through the
     resulting `Calibration.to_world`, reported **(0.10, −0.03) cm** —
     effectively exact, and this point is *not* one of the 10 ArUco
     fit correspondences, so it is a genuine interpolation check, not a
     tautology.
   - East check: ArUco 4 (surveyed east, x=+67) reported a larger
     `to_world(...).x` than ArUco 8 (surveyed west, x=−67): 67.18 vs
     −67.05.
   - North check: ArUco 2 (surveyed north, y=+44.65) reported a larger
     `to_world(...).y` than ArUco 6 (surveyed south, y=−44.65): 44.52
     vs −44.70.
   - `calibration.json` was written under the real `state_dir`; a real
     daemon restart (`aprilcam daemon stop && aprilcam daemon start`)
     reloaded the identical `calibrated_at`/`playfield`/`matched_ids`
     via `get_calibration`, confirming file-backed persistence on
     real hardware too, not only in the `tmp_path`-backed test.

   No sign or axis needed correcting anywhere in this process. Every
   check — synthetic and real — passed on the first implementation,
   which is reported here plainly per this ticket's honesty
   requirement rather than treated as unremarkable.

**Camera-3 field-coverage question — resolved: it was a repositioning
problem, and it has since been resolved.** This ticket's own
description text (mirroring the team-lead's brief) recorded two
different, mutually-inconsistent snapshots of what camera 3 could see
(one said "only ArUco 2, 6, 10"; the ticket body said "1, 2, 4, 5, 7,
10 but not 3, 6, 8, 9") — both now stale. A live capture from the
running daemon during this ticket's implementation
(`aprilcam camera image arducam-ov9782-usb-camera`), run through the
real detector, found **all 10 required ArUco ids (1–10) plus AprilTag
1** already in frame. The stakeholder had evidently already re-aimed
the camera using ticket 002's `camera view` (exactly the tool the
sprint plan built for this purpose) sometime after either of those
snapshots was recorded. Real calibration against the live camera and
the real `main-playfield` succeeded outright (see above) — so as of
this ticket, ticket 007's hardware-verification prerequisite is
already satisfied for the full-coverage case; the required-id set
itself (`main-playfield`'s all-10 requirement) was never the problem —
`required_aruco_ids()` correctly returns all 10 real surveyed ArUco
entries from `main-playfield.json`, confirmed by direct inspection of
that file. Ticket 007 should still perform its own formal pass (its
own headless `@pytest.mark.camera` tests, the occluded-marker and
restart-survival scenarios against real hardware, and the deskewed-view
checks that depend on tickets 004/005/006), but the coverage blocker
this ticket was asked to confirm or rule out is gone.

**Deviations from the plan.** None of substance. `DEFAULT_CALIBRATION_
FRAMES = 30` is carried over from v1 as a named constant but is not
actually consulted by `Calibrator` itself (see its docstring) — the
frame count is entirely the `capture_frames` callable's decision;
`daemon/service.py`'s `CALIBRATION_FRAME_COUNT = 30` is what actually
governs the real daemon's read loop. This was a deliberate boundary
choice (frame *count* policy belongs with frame *access*, which is
handed in, not with the averaging math), not an oversight, but is
recorded here since it means the archived constant name exists in two
places for two different reasons.

**Hotfix (team-lead verification defect): persistent-number addressing
was broken for `calibrate`/`get_calibration`/`get_camera_config`.**
`camera calibrate 3` failed with a misleading `NoPlayfieldLinked`
against a camera `camera list` showed correctly linked. Root cause:
these three methods looked `self._camera_configs`/`self._calibrations`
up keyed by the *raw* CLI/RPC token, but both stores are keyed by slug
only (`set_camera_config`'s `self._camera_configs[config.name] =
config`; `_persist_calibration`/`_load_calibrations` likewise) — a
persistent number never matched, so `calibrate` silently concluded "no
config" instead of resolving to the same camera `camera list`/
`get_frame`/`get_tags` already reach correctly via `_camera_for`.
Fixed with one shared, soft resolver,
`DaemonService._resolve_slug_if_known`, built on the same
`_record_for` `_camera_for` already uses — every camera-config-
addressed method (`get_camera_config`, `calibrate`, `get_calibration`)
now resolves a number-or-slug token to the canonical slug before doing
anything else, so a future camera-config-addressed method inherits
correct addressing automatically instead of re-implementing it.
Deliberately soft (never raises) so it does not disturb
`get_camera_config`'s existing "unknown token" error shape
(`available` = configured slugs, not every registered camera).
`get_frame`/`get_tags`/`get_tag`/`get_tag_history` (and therefore
`view/viewer.py`, which only calls those) were never affected — they
already went through `_camera_for`'s own, correct, number-or-slug
resolution since ticket 021-002. `cli/playfield.py`'s `playfield
config` and `cli/camera.py`'s `camera config` push whole self-naming
documents (the camera identity comes from the document's own `name`
field, never a CLI address token), so neither takes a resolvable token
and neither had this defect. Regression tests added: `calibrate`/
`get_camera_config` by number vs. by slug reach the identical camera,
config, and stored `Calibration`; an RPC-level round trip repeats the
exact live reproduction (`Calibrate` with a numeric `camera` field).
Re-verified live against the stakeholder's running daemon and the
physically-mounted `main-playfield`: `aprilcam camera calibrate 3`
now succeeds (10/10 matched, 0.184 cm reprojection error), and
`get_calibration('3') == get_calibration('arducam-ov9782-usb-camera')`
— exactly one `calibration.json`, under the canonical slug directory,
not a second one keyed `"3"`.

**What ticket 004 needs.** `daemon/detection.py`'s burst pipeline gets
a `Calibration` for a camera the same way `daemon/service.py` does
internally: `DaemonService.get_calibration(camera_slug) -> Optional[
Calibration]` (in-memory cache, populated by a successful `calibrate()`
call or reloaded from `calibration.json` at startup). When it is not
`None`, call `calibration.to_world(pixel)` for each detected corner/
centre exactly as this ticket's tests do (`calibration.to_world(
PixelPoint(x=..., y=...)) -> WorldPoint`) — no additional shift, flip,
or unit conversion is needed on top of that call; the returned
`WorldPoint` is already final A1-centred cm. `Calibration.stale` should
be surfaced (it is always `False` from this ticket — nothing sets it
`True` yet; UC-02's "replacing a playfield definition marks a
calibration against it stale" is not wired this ticket, since nothing
reads `stale` yet either). `world_corners` should be built by mapping
each of a `TagRecord`'s four `pixel_corners` through the same
`to_world` call, preserving the `(front-left, front-right, back-right,
back-left)` order — never independently recomputed. Yaw/heading are
not this ticket's concern (`vision/motion.py`, already built in sprint
021) but should be computed from the *world*-space front edge, after
the `to_world` conversion, not from pixel space.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/vision/
  tests/aprilcam/daemon/test_service.py` — confirm sprint 021's
  uncalibrated detection path is untouched.
- **New tests to write**: `tests/aprilcam/calibration/
  test_calibration.py` (fixture-backed camera replaying baseline
  images from `tests/images/`, plus synthetic ArUco layouts for the
  geometry suite and degenerate-geometry cases); `tests/aprilcam/
  calibration/test_geometry.py` (pure `to_world`/`to_pixel` round-trip:
  `to_pixel(to_world(p)) ≈ p` within ε); `tests/aprilcam/daemon/
  test_service.py` additions for `calibrate`/`get_calibration`
  including the `NoPlayfieldLinked` path and restart-reload.
- **Verification command**: `uv run pytest && uv run mypy` clean; zero
  `typing.Protocol` — `Calibrator`/`Deskewer` derive from their
  declared base classes, `Calibration`/`CalibrationResult` are frozen
  `@dataclass(kw_only=True)` per `calibration/interfaces.py`; baseline
  388 passing, additive only.
