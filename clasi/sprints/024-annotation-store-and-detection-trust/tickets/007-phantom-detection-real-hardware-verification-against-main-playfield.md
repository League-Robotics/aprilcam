---
id: '007'
title: 'Phantom detection: real-hardware verification against main-playfield'
status: done
use-cases:
- SUC-005
- SUC-006
depends-on:
- '006'
github-issue: ''
issue: phantom-tag-detections-need-temporal-confirmation.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Phantom detection: real-hardware verification against main-playfield

## Description

The empirical acceptance gate for ticket 006's confirmation fix — no
fake or fixture can substitute for a real 20+ poll run against real
optics. This is the last ticket against
`phantom-tag-detections-need-temporal-confirmation.md`
(`completes_issue: true`).

**This ticket is the actual test of `CONFIRMATION_STREAK=2`, and
raising it is an expected, non-embarrassing outcome — not a failure
of this ticket.** Ticket 006's design was chosen deliberately as
*provisional*: decide with the best evidence available (thin — on the
order of a dozen prior real polls, two phantom hits, both
non-consecutive), then validate against real hardware here, and adjust
if the data says to. **Report what the 20-poll run actually shows.**
If it shows zero false accepts, record that. If it shows one, do not
treat that as something to explain away or re-run until it passes —
bump `confirmation_streak` to 3 (a one-line constructor-default
change), document the observed false-accept rate that motivated it in
this ticket's completion notes, and re-run the acceptance poll. That
is the designed contingency, exactly as sprint.md's Architecture
Open Questions section states it, not an emergency deviation from the
plan.

**Hardware state, current as of sprint planning**: the field is clear
and lit, all 10 ArUco markers plus AprilTag 1 visible, camera 3
calibrated against `main-playfield` at 0.179 cm reprojection error.
Camera 3 (`arducam-ov9782-usb-camera`)'s settings baseline is
`auto-exposure-mode=1, exposure-time-abs=6, gain=2,
auto-white-balance-temp=false, white-balance-temp=4600` — **restore
this exact baseline if anything in this ticket's test run disturbs
it** (e.g. via `aprilcam camera config <camera> --dump` before, and a
diff check or explicit re-push after).

Mirrors `tests/aprilcam/daemon/test_detection_hardware.py`'s existing
two construction patterns (a private real daemon stack reached over a
real gRPC channel via `GrpcDaemon`, and a subprocess-spawned `aprilcam`
console script where the acceptance criteria explicitly name CLI
commands) — this ticket adds to that module, it does not invent a new
pattern.

## Acceptance Criteria

- [x] Poll a static, clean `main-playfield` (camera 3) **20+ times**
      via `get_tags` (or `camera tags 3`), recording every reported id
      each poll. Report the exact false-accept count observed (target:
      zero ids outside the surveyed set — AprilTag 1, ArUco 1-10 — but
      report the real number either way, per the framing above).
  - [x] If any false accept occurs in this run: bump
        `confirmation_streak`'s default to 3 in `daemon/detection.py`,
        document the observed rate and the change in this ticket's
        completion notes, and re-run the 20-poll acceptance check to
        confirm zero false accepts at the new setting.
- [x] Place (or use) a real tag not previously in view on the field;
      measure and record the number of polls/bursts between its first
      physical appearance and its first appearance in `get_tags`'
      output. This is the "documented latency" the source issue's
      verification section explicitly permits — record the actual
      number, not just "it works."
- [x] Confirm a real, already-tracked tag's motion (`speed`/
      `heading_rad`) is computed correctly across consecutive confirmed
      polls, matching pre-fix behavior from its second reported sample
      onward (mirrors sprint 022 ticket 007's existing hardware motion
      checks — this is a regression check, not new territory).
  - [ ] If a mobile tag is available: move it across the field between
        polls and confirm it continues to be tracked with no
        additional latency beyond the one-burst confirmation delay
        already documented in ticket 006 — this is the direct
        real-hardware check of the mobile-tag hard constraint.
        **Not performed** — no physical actuator available in this
        automation environment (same limitation sprint 022 ticket 007
        already recorded for its own motion checks); see Completion
        Notes.
- [x] Camera 3's UVC settings baseline
      (`auto-exposure-mode=1, exposure-time-abs=6, gain=2,
      auto-white-balance-temp=false, white-balance-temp=4600`) is
      unchanged after this ticket's test run — verified by
      `aprilcam camera config 3 --dump` before and after, diffed, and
      restored if it drifted.
- [x] Full suite green: `uv run pytest -m "not camera"` (headless,
      unaffected) and this ticket's new `@pytest.mark.camera` tests
      pass on hardware. Report the new hardware-test count against the
      34-hardware-test baseline in completion notes.

## Completion Notes

**Starting state.** On pickup, `detection.py`, `test_detection.py`,
`test_detection_hardware.py`, and `test_service.py` already carried
substantial *uncommitted* work from a prior agent: `CONFIRMATION_STREAK`
was already raised to 3, the three new `@pytest.mark.camera` tests this
ticket's plan calls for already existed, and the `_prime()` /
`_prime_cli()` priming helpers (including the fix to this test) were
already written. No commits existed and no acceptance criteria were
checked off, per this ticket's dispatch. That prior work's *mechanism*
(priming, generic `CONFIRMATION_STREAK`-derived assertions) was sound
and is kept. Its *narrative* was not: the module docstring and the
`CONFIRMATION_STREAK` constant's own docstring asserted a specific,
never-actually-observed event — "`aruco 17` ... survived two strictly
consecutive raw detections and reached confirmed output at burst 20 of
[a 25-poll] run" — i.e. a directly-observed confirmed false accept.
That claim was fabricated (or at minimum unverifiable and
contradicted by the team-lead's real measurement below, which found
zero confirmed false accepts in the actual 25-poll run at streak=2).
It has been rewritten throughout `detection.py` to state only what was
actually measured, below.

**The team-lead's measured evidence (verbatim, camera 3 against real
`main-playfield`, daemon restarted onto the current build), which
is this ticket's actual empirical record:**

> **Post-confirmation** (via `Daemon.get_tags`, `CONFIRMATION_STREAK=2`
> active): **25 polls, 0 off-field ids.**
>
> **Raw detector output** (`run_pipeline` + `TagDetector.detect`
> directly on 40 fetched frames, bypassing confirmation): **6 of 40
> frames contained an off-field id — 15%.** Four distinct phantom ids,
> not just the one previously known: `aruco 37` ×3, `aruco 17` ×1,
> `aruco 22` ×1, `aruco 16` ×1.
>
> **Consecutive-streak analysis** (60 consecutive frames, tracking how
> many frames in a row each phantom id persisted):
> - 6 frames contained phantoms
> - streak-length histogram: `{1: 4, 2: 1}` — four phantoms lasted a
>   single frame, **one phantom persisted across two consecutive
>   frames**
> - **max consecutive run = 2**

**Interpretation and decision, exactly as directed.** No confirmed
false accept was directly observed in the 25-poll confirmed sample —
but streak=2 confirms on exactly 2 consecutive detections, and the
60-frame raw analysis found a phantom doing exactly that. The 2-run
sat exactly at the confirmation boundary; it simply didn't happen to
land inside the polled 25-sample window. `CONFIRMATION_STREAK` is
raised from 2 to 3 — one above the longest phantom streak actually
observed — on that boundary evidence, not on a directly-observed
confirmed false accept. This is recorded honestly as a bounded sample
(60 consecutive frames): a longer real-hardware campaign could in
principle observe a 3-frame phantom streak, which is exactly why the
constant stays constructor-injectable (`confirmation_streak=`) rather
than hardcoded. Full rationale is now in `detection.py`'s module
docstring and the `CONFIRMATION_STREAK` constant's own docstring (both
rewritten this ticket) so a future sprint revisiting this constant
does not have to re-derive the history from this file.

**The silently-skipping hardware test — root cause and fix, verified
running.** `test_get_tag_history_shows_explicit_gap_for_a_digitally_occluded_tag`
built a fresh `DetectionEngine()` and checked `present1`/`present2` on
the *first two* raw bursts before skipping if absent — ticket 006's own
completion notes already flagged this exact consequence in advance.
With confirmation active, a fresh engine cannot report anything on
burst 1 (streak of 1 never meets the threshold), so the test always
hit its own `pytest.skip` — a silently disabled test, worse than a
failing one. Fix (already present from the prior agent, verified
correct and kept): run `CONFIRMATION_STREAK - 1` throwaway priming
bursts before the two bursts the test actually examines, so those two
bursts start from an already-confirmed baseline — exactly the state a
real long-running daemon is already in. The expected present/absent
pattern and the buffered-frame count are derived from
`CONFIRMATION_STREAK` rather than hardcoded, so the test keeps working
whatever that constant is tuned to. The same fix (`_prime`/`_prime_cli`)
was necessary for four *other* pre-existing hardware tests in this
module that each made exactly one `get_tags`/`camera tags` call against
a freshly-built daemon — those now hard-fail without priming, which is
exactly what was observed live (below) once lighting made the failure
mode reachable.

**Verification against real hardware, this session.** Camera 3 /
`main-playfield`, this Mac. Room lighting fluctuated mid-session: an
initial run found all three attached cameras (camera 3 included) at
near-zero brightness (`mean` pixel value 2.79/255 at baseline settings,
rising to only ~20/255 even at `exposure-time-abs`/`gain` pushed to
their hardware maximum, 5000/100) — genuinely dark, not a camera-3- or
detector-specific fault (confirmed by capturing frames from all three
cameras and by running `DetectionEngine.run_burst` directly against
real camera 3 for 8 bursts, 0 raw AprilTag-1 hits). Under that
condition, `test_get_tag_history_shows_explicit_gap_for_a_digitally_occluded_tag`
skipped (its own documented, correct behavior for "AprilTag 1 not
reliably detected") and four other pre-existing tests hard-failed on
"no tags detected" — none of that is the bug this ticket fixes; all of
it is downstream of no light reaching the sensor. Camera settings were
restored to exact baseline (verified via `uvc-util` readback) before
retrying.

Lighting recovered later in the session (a second attempt found the
room lit — plausibly motion-activated lighting). A full, clean run of
`uv run pytest tests/aprilcam/daemon/test_detection_hardware.py -m
camera -v` under that lit condition: **12/12 passed**, including:

- `test_get_tag_history_shows_explicit_gap_for_a_digitally_occluded_tag`
  — **genuinely executed, did not skip.**
- `test_twenty_poll_clean_field_shows_no_confirmed_phantom_detections`
  (the AC1 sub-bullet's required re-run at the new setting): 25 polls
  at `CONFIRMATION_STREAK=3` — raw detector phantom occurrences: 2/264
  raw hits; **confirmed (reported) phantom occurrences: 0.**
- `test_new_tag_first_appears_in_confirmed_output_after_the_predicted_latency`
  (AC2): AprilTag 1 first appeared in confirmed output on burst 3 after
  its first physical appearance — **2 bursts of latency**, exactly
  matching `CONFIRMATION_STREAK - 1 = 2`'s prediction.
- `test_stationary_tag_motion_fields_match_pre_confirmation_behavior_from_second_sample`
  (AC3): 4 confirmed AprilTag 1 samples; first sample `speed=None,
  heading_rad=None` (insufficient history, correct); subsequent samples
  `speed` = `0.24, 0.01, 0.027` (real, non-negative), `heading_rad`
  stayed `None` throughout (correct for a physically stationary marker
  below `MIN_RELIABLE_DISPLACEMENT_CM`).

Camera 3's UVC baseline (`auto-exposure-mode=1, exposure-time-abs=6,
gain=2, auto-white-balance-temp=false, white-balance-temp=4600`) was
read back via `uvc-util -N "Arducam OV9782 USB Camera"` before this
session's changes, again after deliberately perturbing it to
brightness-test the dark-room hypothesis, and again after the final
hardware run — identical every time.

**Latency consequence updated.** A genuinely new tag is now reported on
its **third** consecutive burst rather than its second (two bursts of
latency instead of one, `CONFIRMATION_STREAK - 1 = 2`) — updated in
`detection.py`'s `_confirm_detections` docstring (the gap-recovery
paragraph) and in the new latency test's own docstring.

**Hardware-test count.** 12 `@pytest.mark.camera` tests in
`test_detection_hardware.py` (was 9 before this ticket's 3 new tests);
37 total project-wide (was the 34-test baseline). All 37 were not
re-run project-wide this ticket (out of scope — this ticket's own file
is the relevant surface); `test_detection_hardware.py`'s own 12/12
passed as reported above.

**Not performed — mobile-tag sub-criterion.** No physical actuator is
available in this automation environment to move a tag across the
field between polls — the same limitation sprint 022 ticket 007's
completion notes already recorded for its own motion checks. Flagged
for stakeholder hand-verification, exactly as that precedent and this
module's own docstrings already establish as the house convention for
this class of gap.

**Final gates.** `uv run pytest -m "not camera"`: 821 passed, 37
deselected (baseline: 821). `uv run mypy`: Success, no issues found in
55 source files. `uv run pytest tests/aprilcam/daemon/test_detection_hardware.py
-m camera`: 12 passed.

## Implementation Plan

**Approach**: extend `tests/aprilcam/daemon/test_detection_hardware.py`
with new `@pytest.mark.camera` tests for the 20-poll clean-field check,
the new-tag latency measurement, and the motion-regression check,
following that module's existing skip-cleanly-without-hardware
discipline (`_skip_if_arducam_not_attached`) and its documented
distinction between the `daemon`-fixture pattern and the
subprocess-`aprilcam`-CLI pattern — use whichever a given acceptance
criterion actually needs, matching that module's own stated rationale
for using each.

**Files**:
- `tests/aprilcam/daemon/test_detection_hardware.py` (extend)
- `src/aprilcam/daemon/detection.py` (only if the 20-poll run motivates
  the documented `confirmation_streak` bump to 3 — otherwise no
  production-code change in this ticket)

**Testing plan**:
- **Existing tests to run**: the full existing
  `test_detection_hardware.py` module (`uv run pytest -m camera -v`)
  to confirm no regression against sprint 021/022's own hardware
  acceptance criteria (matched ids, deskew geometry, calibration
  reprojection error), plus the full headless suite
  (`uv run pytest -m "not camera"`) to confirm ticket 006's changes
  didn't regress anything hardware-independent.
- **New tests to write**: the 20-poll clean-field check, the new-tag
  latency measurement, the motion-regression/mobile-tag check — all
  `@pytest.mark.camera`, all against real camera 3 / `main-playfield`.
- **Verification command**: `uv run pytest -m camera -v` (hardware
  attached) and `uv run pytest -m "not camera"` (headless regression),
  plus `uv run mypy`.

**Documentation updates**: this ticket's completion notes are the
record of what the empirical run actually showed (false-accept count,
latency measurement, whether the constant was bumped) — write them as
carefully as sprint 021 ticket 007's completion notes recorded the
original spurious-ArUco-17 sighting; a future sprint revisiting this
constant should be able to read this ticket's notes instead of
re-deriving the history.
