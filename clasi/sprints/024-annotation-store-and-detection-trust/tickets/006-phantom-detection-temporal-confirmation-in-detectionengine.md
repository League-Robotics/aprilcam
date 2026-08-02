---
id: '006'
title: 'Phantom detection: temporal confirmation in DetectionEngine'
status: done
use-cases:
- SUC-005
- SUC-006
depends-on: []
github-issue: ''
issue: phantom-tag-detections-need-temporal-confirmation.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Phantom detection: temporal confirmation in DetectionEngine

## Description

Fix the correctness bug where a single false ArUco decode is reported
as an ordinary tag, and — because two spurious sightings at different
positions can be joined by the motion pipeline — sometimes carries a
fabricated `speed`/`heading` for an object that does not exist
(observed live: ArUco 17, not on the field, ~1 poll in 4; also
recorded once in sprint 021's hardware verification).

**Independent of tickets 001-005.** This touches only
`daemon/detection.py`; nothing about the annotation store interacts
with it. It is sequenced here for sprint narrative, not dependency.

### The chosen design, and why — read this before writing code

Four candidate fixes were weighed in the source issue. **Chosen:
presence-based temporal confirmation, applied uniformly to every tag
id, requiring two strictly consecutive detection bursts before a tag
is reported at all.**

**Why presence-based, never position-based — this is the load-bearing
design choice, not an implementation detail.** The observed phantom
sightings appear at *different positions* each time it recurs (the
issue's own example: `(-59.4, -19.6)` in one session, `(50.1, -30.3)`
in another). It would be tempting to confirm a tag by requiring "the
same id at roughly the same place, twice" — but that rule would just
as easily reject a **real, fast-moving robot tag**, whose position
legitimately differs between two consecutive sightings simply because
it moved. That is exactly the hard constraint this fix must not
violate (a robot's tag legitimately appears, moves fast, and leaves).
So confirmation here checks **only** "was this exact tag id detected
in the immediately preceding burst for this camera" — never where.
This is also simpler to implement and reason about than any
position-aware variant would be. **Do not add a position-similarity
check to this logic** — it would look like a reasonable additional
safeguard and would actually reintroduce the exact bug this design
avoids by construction.

**Why uniform across all ids, not a plausibility gate favoring
surveyed markers.** The issue's Cause section raises a third
candidate: hold unsurveyed ids to a higher confirmation bar than
surveyed ones. Rejected — it would require `DetectionEngine` to know a
linked playfield's surveyed ArUco ids at detection time, a new
cross-module dependency (`daemon/detection.py` → playfield
definitions) this fix does not otherwise need, since presence-based
confirmation is playfield-agnostic and already works before a camera
is even linked or calibrated. A uniform rule also means a genuinely
new *surveyed* marker gets the same fast, honest confirmation
treatment as a mobile tag — no special-cased surprise.

**Why not detector parameter tuning (option 2).** Archive was mined
per `CLAUDE.md`'s rule: `archive/src/aprilcam/core/detector.py`'s
widened `adaptiveThreshWinSize*` sweep and subpixel corner refinement
are v1's *only* earned ArUco tuning, and both are already ported (see
`vision/detector.py`'s module docstring). No further
`errorCorrectionRate`/`minMarkerPerimeterRate`-style knob exists
anywhere in v1 to mine. New, untested detector tuning is a separate,
open-ended experimental effort — not this ticket's job, and not
needed: confirmation solves the problem without touching the detector
at all.

**Why not "report confidence" (option 3).** Rejected — pushes the
decision onto every consumer (contradicts the daemon being the single
vision authority) and fails the verification bullet literally: "no id
outside the surveyed set is reported" means *reported*, confidence
field or not.

**Option 4 (suppress motion only) is subsumed, not separately
implemented.** An unconfirmed detection under this design is never
reported at all, so it obviously never carries computed motion either
— no separate motion-suppression code path is needed or wanted.

**Why strictly consecutive (N=2 of M=2), not a wider N-of-M window.**
Simpler to implement and verify, and the observed data supports it:
across the two recorded phantom sightings (sprint 021: 1 of ~8 polls;
this issue: 1 of 4 polls), **neither ever recurred on the immediately
following poll**. A wider window (e.g. "2 of the last 4") is strictly
*weaker* against an independently-random false decode than strict
consecutiveness, for no latency benefit — so it was not chosen.

**`CONFIRMATION_STREAK = 2` is a provisional, honestly-thin-evidence
starting value — not a confirmed-correct constant.** It is informed by
on the order of a dozen real polls total across two sessions, two
phantom hits, zero consecutive — not a large enough sample to prove a
zero-false-accept guarantee from first principles, and this ticket
does not pretend otherwise. **Ticket 007's real-hardware 20-poll run
is the actual empirical gate for this number, not this ticket.** If
that run shows a residual false accept, the documented, expected next
step is raising the constant to 3 — a parameter bump, not a redesign.
Report what the real-hardware data says in ticket 007; there is no
pressure here to confirm 2 is right, and raising it to 3 is not a
failure of this ticket's design, it is the design working as intended
(decide provisionally, validate on hardware, adjust the constant if
the data says to).

**Precedent for the mechanism itself, not just the number.** Mining
`archive/` found the same *idiom* — require N consecutive occurrences
before acting — already used twice in v1: `cli/view_cli.py`'s
`_PROMOTE_FRAMES = 10` (motion-detection debounce) and
`daemon/camera_pipeline.py`'s `_DARK_WINDOW_SAMPLES = 30` (`frame_dark`
debounce). This is this codebase's established way of turning a noisy
single-frame signal into a stable one, not a novel pattern.

### Mechanism

`DetectionEngine` gains per-`(slug, TagId)` consecutive-sighting
counters (e.g. `self._streaks: Dict[str, Dict[TagId, int]]`) and a
module constant `CONFIRMATION_STREAK = 2`, overridable per instance —
matching the existing `drift_tolerance_cm`/`drift_check_interval_s`
constructor-override pattern exactly (name it consistently: a
`confirmation_streak: int = CONFIRMATION_STREAK` constructor
parameter). In `run_burst`:

- A detection whose id was also present in the *immediately preceding*
  burst for that camera slug has its streak incremented.
- A detection with no immediately-prior hit starts a fresh streak at
  1.
- An id absent from this burst has its streak reset to 0 (or removed)
  — a gap of even one burst restarts confirmation. This is an accepted
  trade-off (a flickering *real* tag re-confirms after an occlusion
  gap, adding latency) documented in sprint.md's Migration Concerns,
  not a defect to work around.
- Only detections whose streak has reached `confirmation_streak` are
  built into `TagRecord`s and included in the `TagFrame` this call
  returns **and appends to the ring buffer**. An unconfirmed detection
  is not reported with a pending flag — it is simply absent from this
  burst's output, exactly as if it had not been detected at all.
- No change to `TagRecord`'s shape. No proto/wire change. This is
  entirely internal to `daemon/detection.py`.

### Update the existing test suite — do not skip this

`tests/aprilcam/daemon/test_detection.py` has ~30+ tests, many of
which construct a `DetectionEngine`, run a single burst, and assert a
`TagRecord` appears immediately in that burst's `TagFrame.tags` (e.g.
`test_run_burst_builds_an_honestly_uncalibrated_tag_record`,
`test_calibrated_camera_populates_world_fields_on_first_sighting`,
several of the drift-check tests that inspect `tags` on a single
burst). Once confirmation lands, a single detection alone is no longer
sufficient to produce a `TagRecord` — every one of these tests will
fail unless updated. Two ways to update a given test, pick whichever
fits what it is actually testing:

1. Call `run_burst` **twice** with the same detection present both
   times, and assert against the *second* call's result (this is the
   more honest fix for tests specifically about detection/world-field/
   motion behaviour, since it exercises the real confirmed path).
2. Construct the `DetectionEngine` with `confirmation_streak=1` for
   tests that are genuinely about something else entirely (e.g. ring
   buffer eviction, frame-index monotonicity) and would otherwise need
   an unrelated second-burst rewrite just to keep testing what they
   already test.

This is expected, planned work for this ticket, not a surprise to
discover mid-implementation — budget for it.

## Acceptance Criteria

- [x] A tag id detected in exactly one burst (no immediately-following
      confirming burst) never appears in that burst's `TagFrame.tags`,
      nor in the ring buffer.
- [x] A tag id detected in two strictly consecutive bursts appears in
      the *second* burst's `TagFrame.tags`, not the first.
- [x] A tag id detected, absent for one burst, then detected again
      restarts confirmation (does not carry over a prior streak across
      a gap).
- [x] Confirmation is presence-only: two consecutive detections of the
      same id at **different** positions still confirm normally (this
      is the test that directly proves the mobile-tag hard constraint
      — do not skip it).
- [x] Once confirmed, a tag's steady-state reporting across further
      consecutive bursts is unaffected by this change — a scripted
      detector double producing the same id every burst reports it
      every burst from the second one on, identically to today's
      pre-fix behaviour aside from that one-burst offset.
- [x] Every existing `test_detection.py` test updated per the "Update
      the existing test suite" section above — none deleted, none
      silently left failing/skipped.
- [x] `uv run mypy` clean; zero `typing.Protocol`.
- [x] Full suite green: `uv run pytest -m "not camera"` (baseline: 717
      headless tests passing before this sprint; 811 immediately before
      this ticket's own changes — see completion notes below for why
      the two numbers differ).

## Completion Notes

- **Baseline was 811, not 717** (tickets 001–005 grew the suite by the
  time this one started, matching ticket 005's own completion note).
  This ticket added 10 new tests to `test_detection.py`; the suite now
  reports **821 passed, 34 deselected** (`uv run pytest -m "not camera"
  -q`). `uv run mypy` reports "Success: no issues found in 55 source
  files". Zero `typing.Protocol`.
- **Mechanism**: `DetectionEngine._confirm_detections` (new) keys a
  per-slug `Dict[TagId, int]` streak map. Every burst, each detected
  id's streak becomes `previous.get(id, 0) + 1`; ids absent this burst
  are dropped from the dict entirely (not decremented — removed), so a
  gap restarts at 1 on the next sighting. Only ids whose streak reaches
  `confirmation_streak` (default `CONFIRMATION_STREAK = 2`) pass the
  filter into `_build_tag_record`. `run_burst` calls this once, right
  after `detector.detect(...)`, before building any `TagRecord`.
- **Latency for a genuinely new tag**: exactly one burst — a tag first
  seen in burst *N* is absent from that burst's `TagFrame.tags` and
  first reported in burst *N+1* (assuming it is redetected there), with
  its *N+1* position/pixel/world data, not stale data from burst *N*.
  This is a fixed, one-time cost per fresh appearance (or per
  re-appearance after any gap), independent of polling rate — at a
  human/agent polling cadence (the observed live data was ~1 poll per
  several seconds) this is not perceptible; at a hypothetical
  frame-rate streaming cadence it would be one frame (~33 ms at 30
  fps). No retroactive correction is possible or attempted: the
  dropped first sighting's data is gone, by design (module docstring:
  "not reported with a pending flag").
- **Drift-check independence, a design decision not spelled out in the
  ticket text**: `_maybe_check_drift` (ticket 023-007) is called with
  this burst's *raw* `detections`, not the confirmation-filtered
  subset. Reasoning: `calibration.matched_ids` markers are already a
  trusted, separate mechanism (matched during calibration itself,
  before `DetectionEngine` confirmation ever runs on them), a phantom
  id is by construction never one of them, and gating drift-check
  geometry on confirmation would only add one burst of staleness-
  detection latency for no benefit. This kept ~15
  `test_drift_*`/`test_*_stale*` tests in `test_detection.py` passing
  unmodified — none of them assert on `.tags` contents, only on
  `mark_stale`/`calibration_stale`, so this choice is invisible to them
  either way.
- **Existing-test updates went beyond `test_detection.py`.** The ticket
  scoped "Files" to `daemon/detection.py` and `test_detection.py`, but
  the Definition of Done's actual gate is the *whole* headless suite
  green, and `DaemonService`'s production wiring (`service.py`, itself
  unmodified) constructs `DetectionEngine()` with the default
  `CONFIRMATION_STREAK`. Two more files needed updates as a direct,
  unavoidable consequence:
  - `test_service.py`'s single `_build_tag_service` helper (used by
    `tag_service`, `tag_stub_factory`, and therefore transitively by
    `test_daemon_client.py`'s `tag_client_factory`, ~30+ tests total)
    now passes `confirmation_streak=1` — every test built on it is
    about burst-counting, ring-buffer/history shape, or the RPC wire
    contract, never about confirmation itself, so this is the ticket's
    own sanctioned "genuinely about something else entirely" override,
    applied once at the shared fixture rather than per test.
  - `test_get_tags_on_a_calibrated_camera_reports_real_world_coordinates`
    (the one test exercising `DaemonService`'s real, non-overridden
    `DetectionEngine` against `.tags` contents) was re-burst per the
    ticket's own "more honest fix" pattern: one extra identical frame,
    `get_tags` called twice, assertions moved to the second call.
  - `test_end_to_end_drift_via_the_real_daemon_service_wiring_flips_stale`
    needed no change — it only asserts `calibration_stale`, consistent
    with the drift-check-uses-raw-detections decision above.
  - `test_lifecycle_integration.py` (also named in the ticket's testing
    plan) has no tag-related assertions at all and needed no change.
- **For ticket 007**: `tests/aprilcam/daemon/test_detection_hardware.py`
  (`@pytest.mark.camera`, not part of this ticket's gate) was *not*
  modified — out of this ticket's stated file scope, and it is ticket
  007's own module. Flagging a real behavioural consequence for that
  ticket to account for: `test_get_tag_history_shows_explicit_gap_for_a_digitally_occluded_tag`
  builds a fresh `DetectionEngine()` (default `CONFIRMATION_STREAK = 2`)
  and checks `present1`/`present2` — whether AprilTag 1 appears in the
  *first two* raw bursts — before pytest.skip-ing if not. With
  confirmation now in effect, a fresh engine's first burst can never
  report anything (nothing was ever seen "immediately preceding" it),
  so `present1` will always be `False` and this test will now always
  skip rather than exercise its actual gap-detection logic. It will
  need either an extra priming burst before the present1/present2
  check, or its present-checks shifted by one burst, before it can
  meaningfully run on hardware again. This is real-hardware-only code
  path and could not be verified against actual camera behaviour in
  this sandbox.

## Implementation Plan

**Approach**: add the streak-tracking dict and constant to
`DetectionEngine.__init__`; filter `detections` (or the built
`TagRecord`s) in `run_burst` against the updated streak state before
constructing the returned/appended `TagFrame`. Keep the streak update
and the filter as one clear step in `run_burst`, documented inline
with the presence-not-position reasoning above (future readers of this
code need the "why," not just the "what" — put a condensed version of
the Description's presence-vs-position paragraph directly in this
method's docstring, mirroring how `vision/motion.py`'s
`MIN_RELIABLE_DISPLACEMENT_CM` docstring carries its own derivation
rather than leaving it only in a ticket file).

**Files**:
- `src/aprilcam/daemon/detection.py` (extend `DetectionEngine`)
- `tests/aprilcam/daemon/test_detection.py` (extend with new
  confirmation tests; update existing tests per above)

**Testing plan**:
- **Existing tests to run**: the entire `test_detection.py` module —
  expect broad, planned updates, not a clean pass without changes.
  Also run `tests/aprilcam/daemon/test_service.py` and
  `test_lifecycle_integration.py` to confirm nothing above
  `DetectionEngine` assumed single-burst reporting.
- **New tests to write**: exactly the confirmation-specific criteria
  above (fresh-sighting withheld, second-burst confirmed, gap resets,
  presence-not-position, steady-state unaffected).
- **Verification command**: `uv run pytest -m "not camera"` and
  `uv run mypy`.

**Documentation updates**: `daemon/detection.py`'s module docstring —
add a paragraph on temporal confirmation alongside the existing
"demand-driven, no background thread" and "opportunistic staleness
drift check" sections, in the same style (this module's docstring is
clearly the established place for this class of design note in this
codebase).
