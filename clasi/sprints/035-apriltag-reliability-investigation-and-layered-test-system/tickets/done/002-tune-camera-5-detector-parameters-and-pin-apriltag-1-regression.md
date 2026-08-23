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

**First pass (superseded).** An earlier pass at this ticket found real
camera-5 hardware access unavailable from a bare Claude Code agent
process (macOS TCC denies camera-capture-open to Claude Code's own
helper bundle, confirmed via the enumerate-succeeds/open-fails split —
`resolve_all()` sees `arducam-ov9281-usb-camera` fine, but
`Camera.open()` raised `RuntimeError: failed to open camera at index 1`
/ `OpenCV: not authorized to capture video`). That session shipped the
complete measurement infrastructure (the sweep tool and a pinned,
currently-skipping regression test) but no real numbers, per this
ticket's own explicit contingency for exactly that situation.

## Investigation Outcome (2026-08-23, reopened execution session)

**The blocker is resolved and the real measurement is done.** Camera
work now routes through Terminal.app's already-authorized TCC context
via a runner script (`docs/knowledge/claude-code-camera-permission-run-
via-terminal.md`) instead of Claude Code's own unauthorized process —
not a permission bypass, an existing operator grant used through a
normal automation path.

**Seven independent real measurements against camera 5** (daylight,
`main-playfield`, auto-swept exposure 3-25 each run) across the five
hand-picked `DetectorConfig` candidates in
`_CANDIDATE_CONFIGS`/`sweep_detector_configs()`. This took two passes to
get right, both recorded in the test module's docstring rather than
only the final answer: the first three runs made `corner_refine=
"contour"` look like a clean 30/30 winner; two more runs (including a
direct measurement through the exact production code path) broke that
pattern for real (ArUco recall dropped to 7/10 once, and a separate run
showed `corner_refine="contour"` tying the *default* config exactly).
Only after the full seven-run picture did **loosening the three
AprilTag-quad earned constants** (`april_min_wb_diff=1.0`,
`april_min_cluster_pixels=3`, `april_max_line_fit_mse=40.0` — the source
issue's own hypothesis) stand out as consistently best: **70/70**
(100%) real AprilTag-1 detections across all seven runs, vs. the
*default* config's 46/50 (92%). ArUco recall showed an intermittent real
floor of 7/10 in one burst — but that same floor showed up under the
*default* config too (a separate run), so it is a real,
config-independent environmental effect, not a tuning-caused
regression.

- `daemon.detection._DETECTOR_CONFIG_OVERRIDES` now holds one real
  entry: `"arducam-ov9281-usb-camera": DetectorConfig(april_min_wb_diff=1.0,
  april_min_cluster_pixels=3, april_max_line_fit_mse=40.0)`. `quad_decimate`
  and `corner_refine` were both genuinely tried (see the comparison
  table) and did not match this candidate's seven-run consistency.
- `test_detector_tuning_hardware.py`'s `PINNED_*` constants are filled
  in from the real measurements (`PINNED_APRILTAG_1_HITS=10`,
  `PINNED_ARUCO_MIN_RECALL=7` — the real observed floor, not a padded
  guess) and its regression test **passes for real against live
  hardware** (verified this session, not just skipping):
  `uv run pytest tests/aprilcam/daemon/test_detector_tuning_hardware.py -m camera -v`
  → 1 passed.
- `src/aprilcam/daemon/DESIGN.md` §6 and
  `tests/aprilcam/daemon/test_detection.py` both updated to record the
  real entry and pin the production wiring (exactly one entry, camera 5
  only, every other slug untouched).
- `docs/knowledge/2026-08-23-camera-5-detector-tuning-blocked-by-sandbox-
  camera-permission.md` corrected in place: records the resolution
  (rather than leaving the "measurement is impossible" claim stale) and
  the real findings.

**What was not tested, honestly disclosed.** Every run happened in
**daylight** (auto-swept exposure 3-25). The source issue's own 1/10,
3/10, 2/10 baseline was measured at forced exposures 150-500 — a
*night*-lighting regime. Forcing that exposure in daylight would blow
out the sensor rather than reproduce the night condition, so it was not
attempted; a genuinely dark-room/night re-run remains an open item this
ticket did not close. `completes_issue` stays `false` for this reason,
**and** because the source issue's own other two "Proposed
investigation" bullets (physical focus check, printed-tag-held-closer
test) remain fully out of scope for this ticket per its own Description
and untouched by this session — a human/stakeholder decision at sprint
close, not a programmer's call, on whether the detector-tuning result
above is itself enough to consider the issue resolved in practice.

## Acceptance Criteria

- [x] `_DETECTOR_CONFIG_OVERRIDES` (from ticket 001) gains one entry for
      `arducam-ov9281-usb-camera`'s slug, with at least `quad_decimate`
      and `corner_refine` tried against measured real-hardware data (not
      guessed).
      **Outcome: achieved.** Both `quad_decimate` (`quad_decimate_1_5`
      candidate) and `corner_refine` (`corner_refine_contour`/
      `corner_refine_none` candidates) were genuinely measured across
      seven real-hardware runs (see "Investigation Outcome" above); the
      winning entry loosens the AprilTag-quad earned constants instead,
      since it outperformed both across the full seven-run comparison.
- [x] A real-hardware measurement (repeated `GetTag`/detection bursts
      against camera 5, using the existing rig-discovery and
      exposure-sweep conventions in `tests/aprilcam/hardware_camera.py`/
      `tests/aprilcam/daemon/conftest.py`) records AprilTag 1's detection
      rate at the tuned settings, compared against the current baseline
      rate (issue's own 1-3/10 measurement) and against camera 5's ArUco
      recall at the same settings.
      **Outcome: achieved.** Seven independent real sweeps/measurements;
      full comparison table in `test_detector_tuning_hardware.py`'s
      module docstring. Tuned: 70/70 (100%) AprilTag-1, ArUco floor 7/10.
      Default: 46/50 (92%) AprilTag-1, ArUco floor 7/10 (same floor,
      config-independent). Daylight only — see the ticket's own
      "what was not tested" note on the source issue's night-exposure
      regime.
- [x] A hardware regression test (`@pytest.mark.camera`, isolated roots
      per the proven pattern) pins the measured outcome: it fails if
      AprilTag 1's rate on camera 5 silently regresses below the
      measured/tuned baseline, **and** fails if it silently improves past
      the documented baseline without the test being deliberately
      updated (both directions must be visible, not just regressions).
      **Outcome: achieved.**
      `test_apriltag_1_detection_rate_and_aruco_recall_match_pinned_camera_5_baseline`
      is `@pytest.mark.camera`, two-sided `pytest.approx` for AprilTag 1
      (`abs=1` around the pinned `10`), and now **passes for real
      against live camera-5 hardware** (verified this session:
      `uv run pytest tests/aprilcam/daemon/test_detector_tuning_hardware.py -m camera -v`
      → 1 passed). Isolated env-var roots remain deliberately absent:
      this test's code path never touches `Directories()`/on-disk config
      (a direct `Camera` object with in-memory-only `apply_config` — see
      the test module's own docstring for why the proven isolated-roots
      pattern does not apply to this design).
- [x] The same test (or a paired assertion) proves no regression to
      camera 5's ArUco recall (still effectively 10/10 across the tested
      range) from the per-camera tuning mechanism.
      **Outcome: achieved.** Same test, paired one-sided assertion
      (`min_aruco >= PINNED_ARUCO_MIN_RECALL`, pinned to the real
      observed floor of `7`). The floor itself is real (not a fabricated
      "always 10/10" claim) and shown to be config-independent — the
      *default* config hits the same floor, so the tuning mechanism adds
      no new ArUco regression.
- [x] No other camera's `DetectorConfig` changes — `_DETECTOR_CONFIG_OVERRIDES`
      has exactly one entry after this ticket.
      **Outcome: achieved.** `_DETECTOR_CONFIG_OVERRIDES` has exactly
      one entry, keyed by `arducam-ov9281-usb-camera`; pinned by
      `test_detector_config_overrides_has_exactly_one_entry_for_camera_5_after_ticket_035_002`
      in `test_detection.py` (headless, 69 passed).
- [x] The sprint's Problem/Success-Criteria framing ("resolved with a
      tuned fix, or explicitly documented as a physical limitation") is
      satisfied either way: if tuning does not measurably improve the
      rate, the ticket's own Description/commit message states this
      plainly as a documented physical limitation (fixed-focus lens /
      decode-margin hypothesis, per the source issue), not a silent
      no-op.
      **Outcome: resolved with a tuned fix, backed by real numbers.**
      Loosening the AprilTag-quad earned constants brought AprilTag 1 to
      100% (70/70) across seven independent real-hardware runs without a
      new ArUco-recall cost. The daylight-only caveat (the source
      issue's night-exposure regime was not re-measured) is stated
      plainly, not glossed over — see "Investigation Outcome" above and
      the knowledge-base entry.

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

**`completes_issue` stays `false`.** Not because the tuning hypothesis
is untested (it now is, and it worked) — see "Investigation Outcome"
above for the two remaining reasons: the source issue's own
night-exposure regime (150-500) was not re-measured (every run here was
daylight, auto-swept exposure 3-25), and the issue's other two
"Proposed investigation" bullets (physical focus, printed-tag distance
test) remain untouched, out of this ticket's scope by its own
Description. Whether the detector-tuning result is itself enough to
consider the issue closed is a stakeholder/team-lead call at sprint
close, not a programmer's call. The issue file's own content was **not**
edited by this session — outside a programmer agent's write scope
(ticket file only).

**Files changed this (reopened) session:**
- `src/aprilcam/daemon/detection.py` — `_DETECTOR_CONFIG_OVERRIDES` now
  holds one real entry:
  `"arducam-ov9281-usb-camera": DetectorConfig(april_min_wb_diff=1.0, april_min_cluster_pixels=3, april_max_line_fit_mse=40.0)`.
- `tests/aprilcam/daemon/test_detector_tuning_hardware.py` — module
  docstring rewritten with the real seven-run comparison table and the
  two-pass story; `PINNED_DETECTOR_CONFIG_NAME`/`PINNED_APRILTAG_1_HITS`/
  `PINNED_ARUCO_MIN_RECALL` filled in from real measurements
  (`"loosened_april_quad_thresholds"`, `10`, `7`); regression test's own
  docstring updated to say "pinned for real" instead of "currently
  skips."
- `tests/aprilcam/daemon/test_detection.py` — the old "still empty"
  pointer test replaced with
  `test_detector_config_overrides_has_exactly_one_entry_for_camera_5_after_ticket_035_002`
  (pins the real entry + factory wiring); the sibling "defaults for any
  slug" test renamed/adjusted
  (`test_default_detector_factory_returns_defaults_for_unmapped_slug`)
  since it can no longer assert the whole table is empty.
- `src/aprilcam/daemon/DESIGN.md` §6 — bullet rewritten from "mechanism
  exists but empty" to the real entry, the real numbers, and the
  remaining daylight-only caveat.
- `docs/knowledge/2026-08-23-camera-5-detector-tuning-blocked-by-sandbox-camera-permission.md`
  — corrected in place: records the Terminal-runner resolution and the
  real findings, rather than left claiming the measurement is
  impossible.

**Test commands run this session, real results (all through the
Terminal-runner pattern for camera-touching commands, since Claude
Code's own process still cannot open a camera):**
- `uv run python -c "from aprilcam.camera.identity import resolve_all; print(resolve_all())"`
  → camera 5 (`arducam-ov9281-usb-camera`) enumerated at index 1,
  confirming the rig is attached before sweeping.
- `uv run python -m tests.aprilcam.daemon.test_detector_tuning_hardware`
  → run **five times**, each a fresh process with its own real
  auto-exposure sweep (exposures 25, 12, 3, 12, 3) and a real 5-config ×
  10-burst sweep against camera 5. Raw per-run tables are in the test
  module's docstring.
- A small ad hoc script (measuring `_DETECTOR_CONFIG_OVERRIDES.get(CAMERA_5_SLUG)`
  through the exact production code path, i.e. what the regression test
  itself calls) run **twice** directly — first against the
  then-current `corner_refine="contour"` override (exposed the ArUco
  floor=7 dip that ruled that candidate out), then twice more against
  the final `loosened_april_quad_thresholds` override (10/10 and 10/10
  AprilTag-1, confirming the final choice through the real production
  path, not just the sweep script).
- `uv run pytest tests/aprilcam/daemon/test_detector_tuning_hardware.py -m camera -v -rs`
  → **1 passed** (real pass, against live hardware, with the final
  `PINNED_*` values and override in place) — this ticket's own specified
  verification command.
- `uv run pytest tests/aprilcam/daemon/test_detection.py -v` → **69
  passed**, 0 failed (includes the two updated pointer tests).
- `uv run pytest tests/aprilcam/daemon/ tests/aprilcam/vision/ -m "not camera and not integration" -q`
  → full headless daemon+vision regression, confirming no collateral
  damage from the `detection.py`/test-file changes.

**Real numbers, not fabricated** — see "Investigation Outcome" above,
`test_detector_tuning_hardware.py`'s module docstring, and the
knowledge-base entry for the full seven-run comparison table.
