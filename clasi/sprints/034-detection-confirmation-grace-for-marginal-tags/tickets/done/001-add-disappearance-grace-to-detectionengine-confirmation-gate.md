---
id: '001'
title: Add disappearance-grace to DetectionEngine confirmation gate
status: done
use-cases:
- SUC-001
depends-on: []
github-issue: ''
issue: apriltag-1-detects-unreliably-on-arducam-ov9281-usb-camera.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Add disappearance-grace to DetectionEngine confirmation gate

## Description

`DetectionEngine._confirm_detections`
(`src/aprilcam/daemon/detection.py`) rebuilds its per-slug streak dict
from scratch every burst, keyed only by the tag ids present in *that*
burst. A confirmed tag that misses even one burst therefore drops out
instantly and must re-earn the full `CONFIRMATION_STREAK` (3)
consecutive-burst appearance gate all over again. Combined with a
marginal raw per-burst detection rate (documented root cause:
`clasi/issues/apriltag-1-detects-unreliably-on-arducam-ov9281-usb-camera.md`
— AprilTag 1 on `arducam-ov9281-usb-camera`, raw rate ~0.45–0.6), the
*reported* rate collapses to roughly the raw rate cubed (observed live:
1/12 polls), even though the tag is genuinely present most of the time.

Add a disappearance-grace window that applies **only after** a tag has
already satisfied appearance confirmation: once confirmed, a tag id
keeps its confirmed status through up to `confirmation_grace`
consecutive missed bursts before it is dropped and must re-earn full
`CONFIRMATION_STREAK` confirmation. See sprint.md's Architecture
section ("What Changed" / Design Rationale) for the full mechanism and
the three recorded design decisions (burst-count grace unit, default
`CONFIRMATION_GRACE = 8`, streak-preservation-only — no fabricated or
re-emitted records).

**Mechanism to implement** (sprint.md Architecture, "What Changed"):
replace the single `self._streaks[slug]` rebuild-from-scratch dict with
three pieces of per-slug state:

1. `_streaks` (existing name, narrowed scope) — appearance-building
   counter, updated only for ids not yet confirmed.
2. `_confirmed` (new) — the set of tag ids currently confirmed for that
   slug.
3. `_grace_counters` (new) — for each currently-confirmed id, its
   consecutive-miss count since last actually being detected.

Per burst, for slug `s` with this burst's raw-detection ids
`ids_this_burst`:

- An id in `ids_this_burst` **not yet confirmed**: increment its
  building streak; promote to `_confirmed` once the streak reaches
  `confirmation_streak` (unchanged appearance semantics).
- An id in `ids_this_burst` **already confirmed**: stays confirmed;
  reset its grace counter to 0.
- An id **already confirmed but not in `ids_this_burst`**: increment
  its grace counter. If the incremented count is `<=
  confirmation_grace`, it stays confirmed (silently absent from this
  one burst's output). If it exceeds `confirmation_grace`, drop it
  entirely — no partial credit carried into a future streak.
- An id **not yet confirmed and not in `ids_this_burst`**: dropped
  entirely from the building dict (unchanged today's behavior — a gap
  during build-up restarts from scratch).

The set of ids actually turned into `TagRecord`s for this burst is
`_confirmed ∩ ids_this_burst` (after the above updates) — i.e., a
`TagRecord` is only ever built from a detection that is both currently
confirmed *and* genuinely present in this exact burst. A grace burst
never builds a record, never re-emits a stale one, and never touches
the ring buffer for the tag it is being lenient about.

Add the module constant `CONFIRMATION_GRACE = 8` (evidence-style
docstring matching `CONFIRMATION_STREAK`'s own — see sprint.md Design
Rationale Decision 2 for the geometric argument to restate there) and
the constructor parameter `confirmation_grace: int =
CONFIRMATION_GRACE`, following exactly the pattern
`confirmation_streak` already establishes (stored as
`self._confirmation_grace`, documented in `DetectionEngine.__init__`'s
own docstring).

Update `daemon/detection.py`'s module docstring's "Presence-based
temporal confirmation" section to describe the new grace behavior
alongside the existing streak description, and update
`src/aprilcam/daemon/DESIGN.md`'s "Presence-based tag confirmation,
never position-based — do not 'fix' this" constraint bullet (§3) to
mention that disappearance now has a bounded grace window, so a future
reader doesn't assume a miss still means instant drop.

## Acceptance Criteria

- [x] A tag confirmed once, then missing for `<= confirmation_grace`
      consecutive bursts, is reported immediately on its next genuine
      detection — no re-confirmation delay, no partial streak lost.
- [x] A tag missing for `> confirmation_grace` consecutive bursts is
      dropped from confirmed status and must satisfy the full
      `CONFIRMATION_STREAK` again, from a fresh streak of 1, before
      being reported again.
- [x] No burst ever reports (or buffers) a `TagRecord` for a tag id
      that was not actually present in that exact burst's raw
      detections — grace never fabricates or re-emits a record.
- [x] Appearance-side behavior is byte-for-byte unchanged: every
      existing appearance/phantom-suppression test in
      `tests/aprilcam/daemon/test_detection.py` passes unmodified,
      including the "gap of one burst restarts confirmation" test for
      an *unconfirmed* (building) tag.
- [x] `CONFIRMATION_GRACE == 8` and `DetectionEngine()`'s default
      `self._confirmation_grace` both asserted directly (mirrors
      `test_confirmation_streak_constant_and_default_are_three`).
- [x] `confirmation_grace` is genuinely overridable per instance
      (e.g. `confirmation_grace=0` reproduces exactly today's
      instant-drop-on-any-miss behavior; a larger override tolerates a
      longer gap) — mirrors the existing
      `test_confirmation_streak_is_genuinely_overridable_to_*` tests'
      style.
- [x] `daemon/detection.py`'s module docstring and
      `src/aprilcam/daemon/DESIGN.md` §3 are updated to describe the
      grace mechanism (per Description above).

## Implementation Plan

**Approach**: Modify `DetectionEngine.__init__` and
`DetectionEngine._confirm_detections` in
`src/aprilcam/daemon/detection.py` per the mechanism described above.
Keep `_confirm_detections`'s existing lock discipline (mutate shared
per-slug dicts inside `self._lock`, same as today). Do not touch
`run_burst`, `_build_tag_record`, `_apply_mount_correction`,
`_finish_record`, or any ring-buffer code — this ticket is scoped
entirely to the confirmation-gate method and its constructor
parameter.

**Files to modify**:
- `src/aprilcam/daemon/detection.py` — add `CONFIRMATION_GRACE`
  constant with docstring; add `confirmation_grace` constructor
  parameter and `self._confirmation_grace`/`self._confirmed`/
  `self._grace_counters` instance state; rewrite
  `_confirm_detections`'s body per the mechanism above; update the
  module docstring's "Presence-based temporal confirmation" section
  and `DetectionEngine.__init__`'s own docstring (the
  `confirmation_streak` parameter doc block already there is the
  template to follow for the new parameter).
- `src/aprilcam/daemon/DESIGN.md` — update §3's "Presence-based tag
  confirmation, never position-based" constraint bullet to mention the
  bounded disappearance-grace window.
- `tests/aprilcam/daemon/test_detection.py` — add new tests (see
  Testing below); do not modify any existing test's assertions or
  scripted detector sequences.

**Testing plan**:
- **Existing tests to run** (scoped, per `.claude/rules/source-code.md`
  — do not run the full daemon suite for this ticket):
  `tests/aprilcam/daemon/test_detection.py`. All existing tests in this
  file must continue to pass unmodified — they pin appearance-streak
  behavior (ticket 024-006/024-007) that this ticket must not change.
- **New tests to write**, added to
  `tests/aprilcam/daemon/test_detection.py` near the existing
  "Temporal confirmation" section:
  - `CONFIRMATION_GRACE` constant equals 8; `DetectionEngine()`'s
    default `_confirmation_grace` equals `CONFIRMATION_GRACE` (mirrors
    `test_confirmation_streak_constant_and_default_are_three`).
  - A tag confirmed via `CONFIRMATION_STREAK` consecutive hits, then
    missing for exactly `confirmation_grace` bursts, is reported
    immediately on the next hit after the gap (with no partial-streak
    penalty) — use a small `confirmation_grace` override (e.g. 2) and
    a `_ScriptedDetector` script of `[hit]*CONFIRMATION_STREAK + [[],
    []] + [hit]` to keep the test fast and explicit.
  - The same setup but with `confirmation_grace + 1` consecutive
    misses: the tag is dropped and the next hit does **not** report
    immediately — it must satisfy a fresh `CONFIRMATION_STREAK` again
    (assert the intervening bursts stay empty until the fresh streak
    completes).
  - During a grace burst (tag confirmed, currently missing, within the
    grace window), assert the returned `TagFrame.tags` does not
    include that tag id at all, and that `engine.history(slug)`'s
    corresponding buffered frame also omits it — proving no fabricated
    or re-emitted record is ever built or buffered.
  - `confirmation_grace=0` reproduces exactly today's instant-drop
    behavior (a single miss after confirmation immediately requires
    full re-confirmation) — a regression guard for the old default
    behavior, mirroring
    `test_confirmation_streak_is_genuinely_overridable_to_one_reports_immediately`'s
    style for the new parameter.
  - A steady-state redetection test analogous to
    `test_steady_state_reporting_is_unaffected_by_confirmation_once_past_the_threshold`,
    but interleaving occasional single-burst misses within the grace
    window across many bursts, confirming the tag is reported on every
    burst it is actually detected in and never on a burst it is not.
  - Confirm every *existing* appearance/gap/phantom test in the file
    (e.g. `test_a_gap_of_one_burst_restarts_confirmation_not_carrying_over_a_prior_streak`,
    `test_phantom_recurring_at_different_positions_across_gaps_is_never_reported`,
    `test_unconfirmed_single_sighting_never_appears_in_tags_or_the_buffer`)
    passes unmodified against the new implementation — these exercise
    tags that never reach confirmed status, so grace must not affect
    them; run the full file, not just the new tests, to prove this.
- **Verification command**:
  `uv run pytest tests/aprilcam/daemon/test_detection.py`

**Documentation updates**:
- `src/aprilcam/daemon/detection.py` module docstring — extend the
  "Presence-based temporal confirmation" section with a paragraph on
  the new disappearance-grace behavior, and give `CONFIRMATION_GRACE`
  an evidence-style docstring analogous to `CONFIRMATION_STREAK`'s,
  citing this ticket, the issue file, and the geometric argument
  recorded in sprint.md's Design Rationale (Decision 2).
- `src/aprilcam/daemon/DESIGN.md` §3 — amend the "Presence-based tag
  confirmation, never position-based — do not 'fix' this" bullet to
  note the bounded grace window on the disappearance side, so a future
  reader does not assume any miss still means instant drop.
