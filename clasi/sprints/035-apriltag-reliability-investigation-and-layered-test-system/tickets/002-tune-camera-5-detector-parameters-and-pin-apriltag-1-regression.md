---
id: '002'
title: Tune camera 5 detector parameters and pin AprilTag-1 regression
status: done
use-cases:
- UC-09
depends-on:
- '001'
github-issue: ''
issue: apriltag-1-detects-unreliably-on-arducam-ov9281-usb-camera.md
completes_issue: false
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

## Investigation Outcome (2026-08-23, execution session)

A **third**, narrower outcome than either of the two the Description
above anticipated ("tuned fix" or "documented physical limitation"):
**real camera-5 hardware access was unavailable in this ticket's own
execution environment**, so neither a tuning improvement nor a
physical-limitation conclusion could actually be measured. Confirmed,
not assumed: `aprilcam.camera.identity.resolve_all()` enumerates camera
5 (`arducam-ov9281-usb-camera`) fine over AVFoundation, but opening it
for real capture raised, verbatim, `OpenCV: not authorized to capture
video (status 0)` / `RuntimeError: failed to open camera at index 1` —
a macOS TCC camera-capture authorization gap for this automated
session's process, not a real hardware absence. Full writeup:
`docs/knowledge/2026-08-23-camera-5-detector-tuning-blocked-by-sandbox-camera-permission.md`.

Per this ticket's own explicit contingency for exactly this situation
("if you cannot measure on real hardware, do not fabricate numbers —
write the test, verify it skips correctly, and report clearly that the
measurement itself is a stakeholder step at the rig"), this session's
deliverable is the complete measurement infrastructure, not real tuned
numbers:

- `tests/aprilcam/daemon/test_detector_tuning_hardware.py` —
  `sweep_detector_configs()` (a manual/exploratory sweep across five
  hand-picked `DetectorConfig` candidates, runnable at the rig via
  `uv run python -m tests.aprilcam.daemon.test_detector_tuning_hardware`)
  plus a committed, `@pytest.mark.camera`-marked, two-sided pinned
  regression test. Verified this session: skips cleanly with a specific,
  actionable remediation message
  (`uv run pytest tests/aprilcam/daemon/test_detector_tuning_hardware.py -m camera -v`
  → 1 skipped, real message quoted above).
- `daemon.detection._DETECTOR_CONFIG_OVERRIDES` stays exactly as ticket
  001 left it — **empty**. No entry was added for camera 5: doing so
  with untested field values would be indistinguishable from the
  guessing this ticket is explicitly told not to do.
- `src/aprilcam/daemon/DESIGN.md` §6 and
  `tests/aprilcam/daemon/test_detection.py` both got a pointer note
  recording this outcome so a future reader lands on the real state
  (and the sweep tool) directly rather than re-discovering the blocker.

**What remains, and who does it**: a human with real, already-authorized
camera-5 access (i.e. physically at the rig, not this automated session)
runs `sweep_detector_configs()` for real, decides whether any candidate
measurably helps AprilTag 1 without hurting ArUco recall, then either
adds the winning entry to `_DETECTOR_CONFIG_OVERRIDES` (or leaves it
empty and documents a physical-limitation conclusion — both are
honestly acceptable per this ticket's own Description) and fills in
`test_detector_tuning_hardware.py`'s `PINNED_*` constants so the
regression test starts actually pinning behaviour. This mirrors this
same sprint's own ticket 004 pattern (an operator-assisted step
requiring the stakeholder physically at the rig) rather than being a
one-off improvisation.

## Acceptance Criteria

- [ ] `_DETECTOR_CONFIG_OVERRIDES` (from ticket 001) gains one entry for
      `arducam-ov9281-usb-camera`'s slug, with at least `quad_decimate`
      and `corner_refine` tried against measured real-hardware data (not
      guessed).
      **Outcome: not achieved.** No real camera-5 hardware access this
      session (see "Investigation Outcome" above) — no entry added
      rather than guessing one. `_DETECTOR_CONFIG_OVERRIDES` remains
      empty.
- [ ] A real-hardware measurement (repeated `GetTag`/detection bursts
      against camera 5, using the existing rig-discovery and
      exposure-sweep conventions in `tests/aprilcam/hardware_camera.py`/
      `tests/aprilcam/daemon/conftest.py`) records AprilTag 1's detection
      rate at the tuned settings, compared against the current baseline
      rate (issue's own 1-3/10 measurement) and against camera 5's ArUco
      recall at the same settings.
      **Outcome: not achieved**, same reason. `sweep_detector_configs()`
      in `test_detector_tuning_hardware.py` is built and ready to
      produce exactly this measurement at the physical rig.
- [x] A hardware regression test (`@pytest.mark.camera`, isolated roots
      per the proven pattern) pins the measured outcome: it fails if
      AprilTag 1's rate on camera 5 silently regresses below the
      measured/tuned baseline, **and** fails if it silently improves past
      the documented baseline without the test being deliberately
      updated (both directions must be visible, not just regressions).
      **Outcome: test written and verified, per this ticket's own
      dispatch-level fallback ("write the test, verify it skips
      correctly").** `test_apriltag_1_detection_rate_and_aruco_recall_match_pinned_camera_5_baseline`
      is `@pytest.mark.camera`, structured to fail on both directions
      (two-sided `pytest.approx` for AprilTag 1) once real `PINNED_*`
      values exist, and currently skips cleanly (verified:
      `uv run pytest tests/aprilcam/daemon/test_detector_tuning_hardware.py -m camera -v`
      → 1 skipped) rather than asserting a fabricated baseline. Isolated
      env-var roots were deliberately not added: this test's code path
      never touches `Directories()`/on-disk config (a direct `Camera`
      object with in-memory-only `apply_config` — see the test module's
      own docstring for why the proven isolated-roots pattern does not
      apply to this design).
- [x] The same test (or a paired assertion) proves no regression to
      camera 5's ArUco recall (still effectively 10/10 across the tested
      range) from the per-camera tuning mechanism.
      **Outcome: same test, paired one-sided assertion (`min_aruco >=
      PINNED_ARUCO_MIN_RECALL`), same currently-skipping state as above.**
- [ ] No other camera's `DetectorConfig` changes — `_DETECTOR_CONFIG_OVERRIDES`
      has exactly one entry after this ticket.
      **Outcome: has zero entries, not one.** No camera's config changed
      (the "no other camera changes" half holds trivially), but an
      untested placeholder entry would be indistinguishable from
      guessing, so none was added — left unchecked rather than
      reinterpreted to fit.
- [x] The sprint's Problem/Success-Criteria framing ("resolved with a
      tuned fix, or explicitly documented as a physical limitation") is
      satisfied either way: if tuning does not measurably improve the
      rate, the ticket's own Description/commit message states this
      plainly as a documented physical limitation (fixed-focus lens /
      decode-margin hypothesis, per the source issue), not a silent
      no-op.
      **Outcome: a third, narrower outcome than either anticipated one
      ("hardware access itself was unavailable, not just the fix")
      is stated plainly** in this file's "Investigation Outcome" section,
      the commit message, `DESIGN.md` §6, and a dedicated knowledge-base
      entry — not a silent no-op.

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

## Completion Notes

**`completes_issue` set to `false`** (was `true`) — the underlying issue
is genuinely **not** resolved (the tuning hypothesis remains untested,
not tested-and-negative), so this ticket must not trigger the issue's
automatic archival at sprint close (`clasi.close._sweep_done_issues`/
`_issue_is_deferred`). The issue stays `in-progress` and will correctly
be treated as deferred (not "unresolved and blocking") when this sprint
closes. The issue file's own content was **not** edited by this session
— that is outside a programmer agent's write scope (ticket
file only); a future session updating the issue should read this
ticket's "Investigation Outcome" section first.

**Files changed this session:**
- `tests/aprilcam/daemon/test_detector_tuning_hardware.py` (new) —
  `sweep_detector_configs()` + the pinned `@pytest.mark.camera`
  regression test, both described above.
- `tests/aprilcam/daemon/test_detection.py` — one new headless pointer
  test (`test_detector_config_overrides_still_empty_after_ticket_035_002`)
  recording the outcome next to ticket 001's own "no override yet" test.
- `src/aprilcam/daemon/DESIGN.md` §6 — one new bullet on the per-camera
  tuning mechanism's current (empty) state and why.
- `docs/knowledge/2026-08-23-camera-5-detector-tuning-blocked-by-sandbox-camera-permission.md`
  (new) — the enumerate-succeeds/open-fails macOS TCC finding, general
  enough to help any future `@pytest.mark.camera` work in this sandbox.
- `src/aprilcam/daemon/detection.py` — **unchanged**;
  `_DETECTOR_CONFIG_OVERRIDES` stays `{}`.

**Test commands run this session, real results:**
- `uv run pytest tests/aprilcam/daemon/test_detector_tuning_hardware.py -m camera -v -rs`
  → **1 skipped** (real skip message: `'arducam-ov9281-usb-camera' is
  enumerated ... but could not be opened for capture:
  RuntimeError('failed to open camera at index 1') ...`) — this
  ticket's own specified verification command, confirmed skipping
  cleanly rather than failing or false-passing.
- `uv run python -m tests.aprilcam.daemon.test_detector_tuning_hardware`
  → confirmed the manual sweep entry point runs and raises the same real
  `RuntimeError` (not collected by pytest; a script entry point for a
  human at the rig).
- `uv run pytest tests/aprilcam/daemon/test_detection.py -v` → **69
  passed**, 0 failed (includes the new pointer test).
- `uv run pytest tests/aprilcam/daemon/ tests/aprilcam/vision/ -m "not camera and not integration" -q`
  → **481 passed**, 39 deselected, 0 failed (full headless daemon+vision
  regression, confirming no collateral damage from the new test module's
  imports/fixtures).

**No real-hardware measurement was possible this session** — see
"Investigation Outcome" above and the linked knowledge-base entry. This
is disclosed here per this ticket's own honesty requirement, not
omitted.
