---
id: '007'
title: Hardware verification against main-playfield
status: done
use-cases:
- SUC-003
- SUC-004
- SUC-005
- SUC-006
- SUC-007
depends-on:
- '003'
- '004'
- '005'
- '006'
github-issue: ''
issue: calibration-deskew-and-the-live-view.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Hardware verification against main-playfield

## Description

Implements SUC-007: runs every physical geometry and persistence check
from `sprint.md`'s Success Criteria against real hardware — camera 3
(`arducam-ov9782-usb-camera`) and the physical `main-playfield`.
Mirrors sprint 021's ticket 007 pattern: the only ticket in this sprint
permitted to depend on real detection recall numbers, marked
`@pytest.mark.camera`, and the ticket that closes out the sprint's
linked issue (`completes_issue: true`).

**Prerequisite carried from ticket 003**: camera 3 is known (as of
sprint planning) to see ArUco 1, 2, 4, 5, 7, 10 but not 3, 6, 8, 9 —
short of `main-playfield`'s full required set. Ticket 003's
implementation notes must already record whether that was resolved by
repositioning the camera or by a corrected understanding of the
required set; this ticket **verifies the resolution actually holds**
against the live camera before running the checks below. If it still
doesn't hold when this ticket starts, that is a blocking finding to
surface immediately (physically reposition the camera, or escalate) —
not something to work around by loosening the all-or-nothing
acceptance rule, which is sacrosanct per UC-04.

## Acceptance Criteria

- [x] Camera 3, aimed at the physical field, sees all 10 of
      `main-playfield`'s required ArUco ids; `aprilcam camera calibrate
      arducam-ov9782-usb-camera` succeeds, reporting all 10 matched.
- [x] Physically covering one ArUco marker and re-running calibrate
      fails, naming exactly that marker as missing — not a count, not
      "several missing" (UC-04.S-2 against real hardware, not just
      fixtures). **Verified by digital occlusion, not by physically
      covering a marker by hand** — see Implementation Notes; a hand
      re-verification is suggested but not performed by this agent.
- [x] **AprilTag 1, at the field's physical centre, reports world
      position (0, 0) within tolerance** on a calibrated read
      (`camera tags` or `get_tag`) — the direct physical
      origin-convention check.
- [ ] **Physically moving a tag east increases its reported
      `world.x`; moving it north increases its reported `world.y`.**
      Run against real hardware, not just ticket 004's fixtures — the
      check that actually catches a mirrored field in the field, not
      just in a unit test. **NOT performed — no physical actuator is
      available to this agent.** A real, automated proxy (stationary
      markers at known east/west/north/south positions) passed; see
      Implementation Notes. Needs stakeholder verification by hand.
- [ ] **The same east/north motions move the tag's drawn position
      right and up, respectively, on `aprilcam playfield view`'s live
      deskewed image** — the end-to-end visual confirmation of the
      same invariant. **NOT performed** (same reason). A real
      east/right proxy passed; no reliable real north/up proxy exists
      on this field's current marker layout (boundary markers are
      clipped out of the deskewed render — see Implementation Notes).
      Needs stakeholder verification by hand.
- [x] `aprilcam playfield image -o field.png` against the real camera
      produces an 800×532 PNG whose corners visually correspond to the
      surveyed field corners.
- [x] Stopping and restarting the real daemon (`aprilcam daemon stop &&
      aprilcam daemon start`) leaves the calibration intact — the
      calibration provenance (`calibrated_at`/`playfield`) reported
      before matches what is reported after.
- [ ] `aprilcam camera view` (raw) and `aprilcam playfield view`
      (deskewed) both open and show the live field on this machine —
      the stakeholder-facing "does it actually work" check underlying
      the whole sprint. **Partially verified**: both commands ran
      against the real daemon/camera without erroring or crashing over
      a bounded window, consistent with a live window opening and
      polling successfully — but this agent has no way to see the
      screen and cannot confirm the displayed content is correct.
      Needs stakeholder verification by eye.
- [x] Marked `@pytest.mark.camera` throughout; the full headless suite
      (`uv run pytest -m "not camera"`) stays green without any camera
      attached.

## Implementation Notes

### Priority 1 — two defects diagnosed and fixed

**Defect A: `camera list`/`daemon status` reported `uncalibrated` for a
genuinely calibrated camera.** Root cause: two independent hardcodes,
neither consulting the real calibration store.
`daemon/service.py::DaemonService.list_cameras()` built every
not-held-open camera's `CameraState` with a literal `calibrated=False`;
a held-open camera's state came from `camera/camera.py::Camera.state`,
which *also* hardcoded `calibrated=False`/`calibration_stale=False`
unconditionally (a stale comment — "no calibration subsystem wired this
sprint" — left over from before ticket 003 built one). So no matter
which branch a camera took, its reported calibration status was always
overridden to `False`, while `get_frame(deskewed=True)`/`get_tags`/
`playfield view` all correctly consulted the real
`self._calibrations` store — a pure display/state-reporting bug, never
a calibration bug.

Fix: `list_cameras()` now has one shared overlay point,
`_calibration_flags(slug)`, which reads the real `get_calibration(slug)`
and returns `(calibrated, calibration_stale)` — `(False, False)` for a
never-calibrated camera, `(True, calibration.stale)` otherwise. Applied
to both branches (`dataclasses.replace` for the held-open case, direct
construction for the not-held case), exactly the same overlay pattern
already used for `playfield`. `Camera.state`'s own hardcode is left as
is (by design: `Camera` genuinely has no notion of calibration — that
state lives in `DaemonService`, per the module boundary) but its
docstring now says so explicitly, so a future reader does not
reintroduce the same bug by trusting `Camera.state` directly outside
`list_cameras`.

Real evidence: before restarting the daemon with the fix, the live
system daemon (already calibrated: `calibration.json` on disk with
`reprojection_error_cm=0.180…`, `matched_ids` 1-10) reported:
```
[3]  arducam-ov9782-usb-camera  present  usable  playfield=main-playfield  uncalibrated
```
After restarting with the fix (calibration reloaded from disk, as
before):
```
[3]  arducam-ov9782-usb-camera  present  usable  playfield=-  calibrated
```
(`playfield=-` here is Defect A's fix working correctly alongside the
*separate*, expected restart trap — see the restart-survival scenario
below.) Regression tests:
`test_list_cameras_reports_calibrated_true_for_a_held_open_camera_after_calibrate`
and
`test_list_cameras_reports_calibrated_true_for_a_not_held_camera_after_restart`
in `tests/aprilcam/daemon/test_service.py`.

**Defect B: stationary tags reported a confident `heading` with
`speed=0.00`.** Root cause: `vision/motion.py::compute_motion` always
computed `heading_rad = atan2(vy, vx)` from the two most recent samples,
regardless of how small the displacement between them was — the old
docstring even framed `atan2(0, 0) == 0.0` as a deliberate "no special
case" design choice. A real, physically stationary tag's two most
recent samples are never bit-for-bit identical (sub-pixel detector
jitter between bursts), so `heading_rad` became `atan2` of a tiny,
essentially random displacement vector — a confident-looking, wrong
answer to "which way is this going."

Fix: added `MIN_RELIABLE_DISPLACEMENT_CM = 0.5` to `vision/motion.py`.
Below this two-sample straight-line displacement, `compute_motion`
returns `heading_rad=None` and `velocity=None` (undefined, not zero —
direction is not a small perturbation of a real answer at this
magnitude, it is noise), while `speed` is still returned as a real,
honestly-computed non-negative float (a small speed reading is a
truthful answer to "how fast," unlike a fabricated direction). The
threshold is derived, not guessed: ticket 003's own real-hardware
verification measured this daemon's actual reprojection error at
~0.18-0.19 cm RMS; two independent samples' difference therefore has a
noise floor around `sqrt(2) × 0.18 ≈ 0.25` cm, and 0.5 cm (roughly
double that) gives comfortable margin against additional per-frame
jitter (lighting/exposure/JPEG) that calibration's own multi-frame
averaging smooths away but a single live `get_tags` burst does not. The
full derivation is documented in `vision/motion.py`'s module docstring,
which is where "below what speed is heading meaningless" — the design
question this sprint necessarily had to answer — is actually decided
and reasoned through. `daemon/detection.py`'s module docstring
cross-references it. The CLI (`cli/camera.py::_format_tag_row`) was
also fixed to print `yaw_rad` (previously silently absent from
`camera tags` output despite `TagRecord` carrying it since ticket
022-004) ahead of `heading`/`speed` — for a stationary tag, `yaw` (the
tag's own facing) is the meaningful orientation; `heading` (motion
direction) is not, and now correctly reads as undefined (`-`) rather
than a fabricated angle.

Real evidence: before the fix, three real markers on the live daemon
read (among others):
```
aruco 8   world=(-67.1, -0.1)  yaw=-0.087  heading=-1.523  speed=0.00
aruco 4   world=(67.2, 0.0)    yaw=0.003   heading=-0.778  speed=0.00
```
— confident, differing headings for markers that had not moved. After
restarting with the fix, three consecutive real `camera tags` bursts
against the same genuinely-stationary markers showed `heading=-`
(undefined) throughout, with `speed` honestly small and non-negative
(0.00-0.08 cm/s) and `yaw` populated, e.g.:
```
aruco 8   world=(-67.0, -0.1)  yaw=-0.003  heading=-  speed=0.05
aruco 4   world=(67.1, -0.0)   yaw=0.010   heading=-  speed=0.07
```
Regression tests: `tests/aprilcam/vision/test_motion.py` (renamed
`test_zero_motion_case_reports_zero_speed_and_undefined_heading` plus
three new boundary tests: just-below, just-above, and exactly-at the
threshold) and `tests/aprilcam/cli/test_camera_cli.py` (yaw-display and
stationary-tag-rendering tests).

### Priority 2 — acceptance scenarios, run for real

All against camera 3 (`arducam-ov9782-usb-camera`) aimed at the
physical `main-playfield`, using the real system daemon
(`config_dir=/Users/eric/.config/aprilcam`,
`state_dir=/Users/eric/.local/share/aprilcam`) — stopped and restarted
with the fixed code before this pass began, and restored to its
original running/calibrated state afterward.

- **Full-set calibration**: `aprilcam camera calibrate
  arducam-ov9782-usb-camera` succeeded on every run, 10/10 ArUco ids
  matched (1-10). Reprojection error across three separate real runs
  during this ticket: 0.208 cm, 0.186 cm, 0.179 cm — consistent with
  ticket 003's 0.180-0.187 cm baseline.
- **Occluded-marker failure**: verified by digital occlusion (real
  frames, real detector, one real marker's known pixel region blacked
  out before reaching the pipeline — matching sprint 021's own
  established precedent for exactly this situation, disclosed in
  `tests/aprilcam/daemon/test_calibration_hardware.py`'s module
  docstring). Real result: `succeeded=False`,
  `missing_ids=(8,)` exactly — not a count, not "several missing" —
  with the other 9 ids in `matched_ids`. **Physically covering a marker
  by hand was not performed** (no actuator available to this agent) —
  suggested as a hand re-verification, not required to trust this
  result given the substitution matches established project precedent.
- **Origin check**: AprilTag 1 read `world=(0.1, -0.1)` cm, consistently
  across every query burst during this ticket — well within tolerance
  of (0, 0), consistent with the team-lead's own (0.1, −0.0) and ticket
  003's (0.10, −0.03).
- **East/north checks (static proxy — see the two unchecked boxes
  above)**: real, stationary markers at known east/west/north/south
  positions read correctly signed and ordered: ArUco 4 (east, surveyed
  x=+67) read `world.x` 67.0-67.2, ArUco 8 (west, x=-67) read -67.0 to
  -67.1; ArUco 2 (north, y=+44.65) read `world.y` 44.4-44.6, ArUco 6
  (south, y=-44.65) read -44.7 to -44.8. This confirms the axis
  convention against real hardware but is **not** the literal
  acceptance criterion, which asks for a tag physically moved — not
  performed; flagged for the stakeholder.
- **Deskewed-view direction (partial)**: re-detecting directly on the
  real deskewed PNG's own pixels (not reusing the raw-frame detection)
  confirmed ArUco 10 (east-internal, x=+35) drawn right of ArUco 9
  (west-internal, x=-35) — a real, independent, pixel-level east/right
  confirmation. The north/up half could not be automated: redetecting
  on the deskewed image found only `apriltag 1`/`aruco 9`/`aruco 10` —
  every other marker (all of main-playfield's north/south pairs are
  also its boundary/corner markers, surveyed within ~0.15 cm of the
  field's own half-width/half-height) is clipped out of the 800×532
  deskewed render. This is a genuine finding about the current physical
  rig, not a code defect (deskew cropping was ticket 005's scope, out
  of this ticket's mandate to change) — flagged for the stakeholder,
  who may want to either verify north/up by eye on `playfield view` or
  consider surveying/relocating a north/south reference pair further
  from the boundary.
- **Deskew dimensions**: `aprilcam playfield image -o field.png`
  produced exactly an 800×532 PNG, `px_per_cm=5.957` (matching the
  ticket's expected value exactly). Visually inspected directly: a real
  photograph of the physical wooden field, with ArUco corner markers
  visibly cut off right at all four image edges (consistent with the
  corners being surveyed essentially at the field's physical boundary,
  and with the deskewed-redetection finding above) and the colored
  rectangles/dots from `main-playfield.json`'s decorative feature set
  visible in matching relative positions — genuine visual
  correspondence confirmed.
- **Restart survival**: stopped and restarted the real system daemon
  (`aprilcam daemon stop && aprilcam daemon start`). Calibration
  survived byte-for-byte — `calibrated_at`, `playfield`, `matched_ids`,
  and `reprojection_error_cm` all identical before and after, confirmed
  by querying `get_calibration` through the real production client
  against the real daemon both times. **Confirmed the documented trap**:
  the camera→playfield *link* (and the playfield *definition* itself —
  both pushed config, both in-memory only) did **not** survive.
  Immediately after restart, `daemon status` showed
  `arducam-ov9782-usb-camera` as `playfield=- calibrated` — and a
  deskewed request in that state failed:
  ```
  error: 'arducam-ov9782-usb-camera' was calibrated against playfield
  'main-playfield', which this daemon no longer holds
  ```
  This is a real, confusing interaction worth flagging plainly:
  `calibrated=True` alone does not mean "ready for a deskewed request"
  after a restart — the playfield and the camera-config link both have
  to be re-pushed first, even though the calibration survived. Also
  committed as a real subprocess-CLI regression test
  (`test_calibration_survives_a_restart_but_the_playfield_link_does_not`).
- **`NotCalibrated`**: confirmed live against `hd-usb-camera` (attached,
  never calibrated): `aprilcam playfield image --camera hd-usb-camera`
  failed with `error: 'hd-usb-camera' is not calibrated`, exit code 1.
  Also committed as a regression test.
- **`camera view`/`playfield view`**: both launched successfully
  against the real daemon and camera via the real CLI; neither crashed
  nor printed an error within a bounded ~8 s window, consistent with a
  live window opening and running its poll loop without incident. This
  agent has no way to see a screen, so it **cannot** confirm the
  displayed content is a correct live rendering of the field — flagged
  plainly for the stakeholder to check by eye, per this ticket's
  honesty requirement.

### Test/mypy results

- `uv run pytest -m "not camera"`: **606 passed**, 24 deselected (599
  baseline + 7 new headless tests: 3 in `test_motion.py` net of one
  rename, 2 in `test_service.py`, 2 in `test_camera_cli.py`).
- `uv run mypy`: **Success: no issues found in 54 source files.**
- `uv run pytest -m camera` (this Mac, arducam attached and aimed at
  `main-playfield`): **24 passed**, 0 failed (16 pre-existing hardware
  tests + 8 new in `tests/aprilcam/daemon/test_calibration_hardware.py`).

### What the stakeholder must still verify by hand

1. Physically cover one ArUco marker and re-run `aprilcam camera
   calibrate` — confirm it still fails naming exactly that marker (this
   agent only verified the digital-occlusion equivalent).
2. Physically move a tag east/north and confirm `world.x`/`world.y`
   increase, live — this agent only verified the static proxy (real
   stationary markers at known positions).
3. Watch `aprilcam playfield view` while physically moving a tag east
   and north and confirm the drawn position moves right and up — this
   agent could only confirm the east/right half via redetection on a
   static deskewed image, and could not test north/up at all (the only
   north/south markers on this field are clipped out of the deskewed
   render).
4. Eyeball `aprilcam camera view` and `aprilcam playfield view` and
   confirm the displayed video is genuinely a correct live rendering of
   the field — this agent confirmed both commands run without error but
   cannot see a screen.

## Testing

- **Existing tests to run**: the full headless suite (`uv run pytest
  -m "not camera"`) must be unaffected — this ticket adds hardware-only
  tests, nothing else.
- **New tests to write**: `tests/aprilcam/daemon/
  test_calibration_hardware.py` (matching sprint 021's
  `test_detection_hardware.py` naming) — one test per acceptance
  criterion above, each `@pytest.mark.camera`, each skipping cleanly
  (not failing) when no camera is attached.
- **Verification command**: `uv run pytest && uv run mypy` clean
  (headless); `uv run pytest -m camera` on a machine with camera 3
  attached and aimed at `main-playfield` for the hardware-marked
  subset; baseline 599 passing headless plus this ticket's additions
  (606 headless, 24 camera-marked), all green — see Implementation
  Notes for the real observed run. (The "388" figure this line
  previously carried was a stale template leftover from an earlier
  sprint's ticket, not this sprint's actual baseline.)
