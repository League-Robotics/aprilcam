---
id: '002'
title: Tune camera 5 detector parameters and pin AprilTag-1 regression
status: open
use-cases: [UC-09]
depends-on: ['001']
github-issue: ''
issue: apriltag-1-detects-unreliably-on-arducam-ov9281-usb-camera.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Tune camera 5 detector parameters and pin AprilTag-1 regression

## Description

AprilTag 1 decodes on only 1-3 of 10 polls on camera 5
(`arducam-ov9281-usb-camera`) across a 150-500 exposure range, while all
10 ArUco markers decode on effectively every poll on the same frames.
Exposure has already been ruled out as the sole cause (no clean
monotonic trend). This ticket investigates whether per-camera
`vision.detector.DetectorConfig` tuning (`quad_decimate`, `corner_refine`,
and if warranted the `april_min_wb_diff`/`april_min_cluster_pixels`/
`april_max_line_fit_mse` earned constants) measurably improves AprilTag
1's detection rate on camera 5 without regressing camera 5's ArUco
recall or any other camera's detection — and pins whatever the honest
outcome is with a regression test.

**This ticket's acceptance does not require a clean fix.** Per the
source issue's own note ("Not blocking — ArUco alone gives full
playfield coverage"), the most likely honest outcome is "documented
physical limitation + tuned parameters + a regression test pinning
current behavior," and that is an acceptable outcome here — the
physical checks named in the issue (lens focus, a printed tag held
closer) are explicitly out of scope for this ticket; they are a
stakeholder/operator step recorded in the sprint's Scope section, not
something this ticket can perform.

## Acceptance Criteria

- [ ] `_DETECTOR_CONFIG_OVERRIDES` (from ticket 001) gains one entry for
      `arducam-ov9281-usb-camera`'s slug, with at least `quad_decimate`
      and `corner_refine` tried against measured real-hardware data (not
      guessed).
- [ ] A real-hardware measurement (repeated `GetTag`/detection bursts
      against camera 5, using the existing rig-discovery and
      exposure-sweep conventions in `tests/aprilcam/hardware_camera.py`/
      `tests/aprilcam/daemon/conftest.py`) records AprilTag 1's detection
      rate at the tuned settings, compared against the current baseline
      rate (issue's own 1-3/10 measurement) and against camera 5's ArUco
      recall at the same settings.
- [ ] A hardware regression test (`@pytest.mark.camera`, isolated roots
      per the proven pattern) pins the measured outcome: it fails if
      AprilTag 1's rate on camera 5 silently regresses below the
      measured/tuned baseline, **and** fails if it silently improves past
      the documented baseline without the test being deliberately
      updated (both directions must be visible, not just regressions).
- [ ] The same test (or a paired assertion) proves no regression to
      camera 5's ArUco recall (still effectively 10/10 across the tested
      range) from the per-camera tuning mechanism.
- [ ] No other camera's `DetectorConfig` changes — `_DETECTOR_CONFIG_OVERRIDES`
      has exactly one entry after this ticket.
- [ ] The sprint's Problem/Success-Criteria framing ("resolved with a
      tuned fix, or explicitly documented as a physical limitation") is
      satisfied either way: if tuning does not measurably improve the
      rate, the ticket's own Description/commit message states this
      plainly as a documented physical limitation (fixed-focus lens /
      decode-margin hypothesis, per the source issue), not a silent
      no-op.

## Implementation Plan

**Approach**: Use the existing session-cached exposure-sweep pattern
(`tests/aprilcam/daemon/conftest.py::hardware_test_camera_config`) as a
model — sweep a small grid of `quad_decimate`/`corner_refine` values
against camera 5 for real, counting AprilTag-1 detections per burst,
keep whichever setting detects most without visibly hurting ArUco
recall. Wire the winning (or, if none help, the current-default)
`DetectorConfig` into `_DETECTOR_CONFIG_OVERRIDES` from ticket 001.

**Files to modify**:
- `src/aprilcam/daemon/detection.py` — one entry in
  `_DETECTOR_CONFIG_OVERRIDES`.
- New hardware test file (e.g.
  `tests/aprilcam/daemon/test_detector_tuning_hardware.py`) or an
  addition to an existing hardware-marked module — the regression test
  and the measurement helper.
- `src/aprilcam/daemon/DESIGN.md` — one short note recording that camera
  5 carries a non-default `DetectorConfig` and why (mirrors how other
  earned constants are documented elsewhere in this codebase).

**Testing plan**:
- Real-hardware measurement sweep (manual/exploratory, not itself a
  committed test) to pick tuning values.
- New committed regression test, `@pytest.mark.camera`, isolated per the
  proven pattern, skip-cleanly if camera 5 is not attached.
- **Verification command**: `uv run pytest tests/aprilcam/daemon/test_detector_tuning_hardware.py -m camera -v`
  (full-suite `camera`-marked tests are not run here — this is a
  fixture-independent, hardware-only ticket; the module's own tests are
  the scoped run).

**Documentation updates**:
- `docs/knowledge/` — if the investigation produces a non-obvious
  finding (e.g., "quad_decimate=X measurably helps/does not help on this
  sensor"), record it per CLAUDE.md's knowledge-base convention so this
  is not rediscovered.
- `clasi/issues/apriltag-1-detects-unreliably-on-arducam-ov9281-usb-camera.md`
  — the issue's own Current State / Proposed investigation sections
  reflect the resolved-or-documented outcome (handled by ticket
  close/issue archival, not a separate manual edit).
