---
id: '007'
title: Hardware verification against main-playfield
status: open
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

- [ ] Camera 3, aimed at the physical field, sees all 10 of
      `main-playfield`'s required ArUco ids; `aprilcam camera calibrate
      arducam-ov9782-usb-camera` succeeds, reporting all 10 matched.
- [ ] Physically covering one ArUco marker and re-running calibrate
      fails, naming exactly that marker as missing — not a count, not
      "several missing" (UC-04.S-2 against real hardware, not just
      fixtures).
- [ ] **AprilTag 1, at the field's physical centre, reports world
      position (0, 0) within tolerance** on a calibrated read
      (`camera tags` or `get_tag`) — the direct physical
      origin-convention check.
- [ ] **Physically moving a tag east increases its reported
      `world.x`; moving it north increases its reported `world.y`.**
      Run against real hardware, not just ticket 004's fixtures — the
      check that actually catches a mirrored field in the field, not
      just in a unit test.
- [ ] **The same east/north motions move the tag's drawn position
      right and up, respectively, on `aprilcam playfield view`'s live
      deskewed image** — the end-to-end visual confirmation of the
      same invariant.
- [ ] `aprilcam playfield image -o field.png` against the real camera
      produces an 800×532 PNG whose corners visually correspond to the
      surveyed field corners.
- [ ] Stopping and restarting the real daemon (`aprilcam daemon stop &&
      aprilcam daemon start`) leaves the calibration intact — the
      calibration provenance (`calibrated_at`/`playfield`) reported
      before matches what is reported after.
- [ ] `aprilcam camera view` (raw) and `aprilcam playfield view`
      (deskewed) both open and show the live field on this machine —
      the stakeholder-facing "does it actually work" check underlying
      the whole sprint.
- [ ] Marked `@pytest.mark.camera` throughout; the full headless suite
      (`uv run pytest -m "not camera"`) stays green without any camera
      attached.

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
  subset; baseline 388 passing plus this ticket's additions, all green.
