---
id: '007'
title: Hardware verification against main-playfield
status: open
use-cases: [SUC-001, SUC-002, SUC-003, SUC-004, SUC-005]
depends-on: ['002', '005', '006']
github-issue: ''
issue: tag-detection-and-tag-queries.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Hardware verification against main-playfield

## Description

The sprint's ground-truth check: everything built in tickets 001-006
against fixtures and fakes, exercised for real against the 4 attached
cameras (`brio-501`, `hd-usb-camera`, `arducam-ov9782-usb-camera`,
`usb2-video`) and the physical `main-playfield` (AprilTag 1 at centre,
ArUco 1-8 perimeter + 9-10 internal, per
`config/aprilcam-old/playfields/main-playfield.json` — the field's
declared definition; note `tests/images/field-state.json` itself is
stakeholder-unconfirmed, so this ticket's assertions are against the
playfield's *declared* set, not that file). All tests here are
`@pytest.mark.camera` and must skip cleanly on a machine with no
cameras attached — this is the **last** ticket, closing out the whole
`tag-detection-and-tag-queries.md` issue (`completes_issue: true`).

## Acceptance Criteria

- [ ] With at least one camera aimed at `main-playfield`, `get_tags`
      (via the daemon, driven through the CLI or client library)
      detects AprilTag 1 and a plausible subset of ArUco 1-10 (full
      recall is not required — real lighting/framing varies — but
      family and id correctness for whatever is detected is required).
- [ ] `TagId` family/number correctness: no result reports a negative
      id anywhere in the client-visible output (confirms ticket 003's
      Decision 2 — no `-(N+1)` leak).
- [ ] Every returned record reports `calibrated=False`, `world=None`,
      usable `pixel_corners` — never zeros dressed as world
      coordinates (re-verifies ticket 004's core contract against
      real detector noise, not just fixtures).
- [ ] The boundary/hat corner convention (ticket 003's verified
      hypothesis) is checked against at least one **known-orientation**
      physical tag — confirm the "front" edge as reported matches the
      tag's actual physical mounting, cross-checking ticket 003's
      documented convention rather than assuming the fixture-based
      verification generalizes to real optics/lens distortion.
  - [ ] If this check reveals the convention from ticket 003 was
        wrong, this ticket fixes the corner-reorder logic (in
        `daemon/detection.py`, per ticket 004's implementation) and
        updates `vision/detector.py`'s docstring accordingly — do not
        merely note the discrepancy and move on.
- [ ] `get_tag_history` on a tag that is moved out of frame and back
      shows an explicit gap (an entry with an empty `.tags` match for
      that tag between two present entries), not interpolated
      positions and not a shortened list.
- [ ] An idle daemon (camera open, zero `get_tags`/`get_tag`/
      `get_tag_history` calls in flight) shows no detection-attributable
      CPU activity over a real 10 s window — the daemon-process CPU
      stays at its sprint-020 idle baseline (raw-frame-only capture);
      record the observed number as this ticket's regression pin, per
      dev-plan Stage 8's "regression-pinned numbers on this Mac, not
      absolutes."
- [ ] `camera image 3`/`camera tags 3` (an actual registry number
      assigned on this machine) and the same commands using that
      camera's slug produce results from the same physical camera
      (re-verifies ticket 002 against real hardware, closing the
      sprint-020 acceptance leg that UC-25.S-5 could not verify without
      hands on hardware).
- [ ] `aprilcam camera tags <camera>` output is legible and correctly
      shows the uncalibrated flag against a real camera, not just a
      fake backend.
- [ ] All new tests in this ticket are `@pytest.mark.camera` and skip
      cleanly (not fail, not error) when no camera hardware is present
      — verify with `uv run pytest -m "not camera"` still passing
      headless.
- [ ] `uv run mypy` stays clean; `uv run pytest` (full suite,
      hardware-marked tests included when run on this Mac) stays
      green.

## Testing

- **Existing tests to run**: the full headless suite
  (`uv run pytest -m "not camera"`) must be unaffected by this
  ticket's additions; `uv run mypy`.
- **New tests to write**: a hardware test module (e.g.
  `tests/aprilcam/daemon/test_detection_hardware.py`, or grouped with
  wherever this sprint's other `@pytest.mark.camera` tests naturally
  live) covering every acceptance criterion above, each individually
  skippable and independently diagnosable (do not bundle everything
  into one giant test where a single assertion failure hides which
  criterion actually broke).
- **Verification command**: `uv run pytest -m camera -v` (on this Mac,
  with cameras attached and `main-playfield` in view) and
  `uv run pytest -m "not camera"` (confirms headless is unaffected).

## Implementation Plan

### Approach

1. Start the real daemon (or drive `DaemonService` directly against
   real `Camera`/backend instances — match whichever pattern the
   sprint's other hardware-marked tests use, if any precedent exists
   from sprint 020; otherwise establish the pattern here, keeping it
   consistent with `test_service.py`'s existing fixture style).
2. Physically verify (a person, not the test) that `main-playfield`
   is in view of at least the cameras this ticket exercises before
   running — this is manual bring-up, not something the test can
   assert about the physical world.
3. Write each acceptance criterion as its own test function so a
   failure names exactly what broke.
4. If the corner-order convention needs fixing (see the conditional
   acceptance criterion above), make the fix in `daemon/detection.py`
   and re-run tickets 003/004's existing headless test suites to
   confirm nothing there regresses.
5. Record the observed idle-CPU number from the 10 s window
   acceptance criterion directly in this ticket's file (or a short
   code comment on the regression test) so it is a documented,
   traceable baseline, not a number that exists only in a CI log.

### Files to create/modify

- `tests/aprilcam/daemon/test_detection_hardware.py` (new, or
  wherever this sprint's `@pytest.mark.camera` convention places it)
- Possibly small fixes to `src/aprilcam/daemon/detection.py` or
  `src/aprilcam/vision/detector.py` if the corner-order convention
  needs correcting against real hardware (see conditional acceptance
  criterion)

### Documentation updates

If the corner-order convention is corrected, `vision/detector.py`'s
docstring (ticket 003) is updated to match — this is the one
documentation update this ticket may need to make outside its own
test file, and only conditionally.
