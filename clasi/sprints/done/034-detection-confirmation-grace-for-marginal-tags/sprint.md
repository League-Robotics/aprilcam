---
id: '034'
title: Detection confirmation grace for marginal tags
status: done
branch: sprint/034-detection-confirmation-grace-for-marginal-tags
use-cases:
- SUC-001
issues:
- apriltag-1-detects-unreliably-on-arducam-ov9281-usb-camera.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 034: Detection confirmation grace for marginal tags

## Goals

Add disappearance grace to `DetectionEngine._confirm_detections` so a
confirmed tag id keeps being reported through a bounded number of missed
detection bursts, instead of dropping instantly on a single miss and
paying the full re-confirmation streak. Appearance-side phantom
suppression (the existing `CONFIRMATION_STREAK = 3` consecutive-burst
gate) is preserved unchanged. The grace bound is constructor-injectable,
mirroring `confirmation_streak`, with a documented default.

## Problem

`clasi/issues/apriltag-1-detects-unreliably-on-arducam-ov9281-usb-camera.md`
carries the full root-cause analysis (2026-08-23). AprilTag 1 detects
unreliably on the `arducam-ov9281-usb-camera` (mono/global-shutter)
while all 10 ArUco markers on the same frames detect solidly. A full
v1-vs-v2 detector comparison found the detectors themselves
byte-identical (same DICT_APRILTAG_36h11, same adaptive-threshold
windows, same CORNER_REFINE_SUBPIX, same input pipeline) — detector
parameters are not the cause and should not be tuned.

The real cause is in `daemon/detection.py`'s confirmation gate:

1. `CONFIRMATION_STREAK = 3` requires a tag id to appear in 3 strictly
   consecutive bursts before it is reported at all. Reported rate ≈
   raw_rate³: AprilTag 1's marginal raw rate (~0.45-0.6) collapses to
   the observed 1-3/10 polls, while ArUco's high raw rate (~0.97) stays
   "solid" (~0.91) under the same cube.
2. `_confirm_detections` has **no disappearance hysteresis** — the
   streak map is rebuilt each burst from only the ids present, so a
   confirmed tag that misses just one burst drops instantly and pays
   the full 3-burst re-confirmation cost again. Combined with a
   marginal raw rate, this makes the tag effectively invisible even
   though it is genuinely present most of the time.

The 3-burst appearance streak exists on real evidence (ticket 024-007:
6/40 raw frames carried phantom off-field ids) and must stay unchanged
for appearance. The fix is disappearance-side only.

## Solution

Add a disappearance-grace mechanism to `_confirm_detections`: once a
tag id is confirmed, continue reporting it through up to N missed
bursts (unit — burst count vs. time horizon — is a detail-time
decision) before resetting its confirmation state. Appearance
continues to require `CONFIRMATION_STREAK` consecutive hits, unchanged.
The grace bound is constructor-injectable like `confirmation_streak`,
with a documented default chosen to meaningfully help a ~0.5 raw-rate
tag without materially delaying detection of genuine disappearance.

## Success Criteria

- A confirmed tag with a marginal raw detection rate (e.g. AprilTag 1
  on `arducam-ov9281-usb-camera`, raw ~0.45-0.6) is reported far more
  consistently across bursts than today's cubic collapse, without any
  change to how quickly a genuinely new/phantom id gets confirmed.
- Appearance-side phantom suppression is unchanged: an unconfirmed id
  still needs `CONFIRMATION_STREAK` consecutive bursts to be reported.
- `tests/aprilcam/daemon/test_detection.py` covers: grace keeps
  reporting through bursts within the bound, resets and requires full
  re-confirmation once the bound is exceeded, and appearance-streak
  behavior is unaffected.
- The grace bound is a documented, constructor-injectable parameter
  with a sensible default, matching how `confirmation_streak` is
  already exposed.

## Scope

### In Scope

- `src/aprilcam/daemon/detection.py` — `DetectionEngine._confirm_detections`
  and its constructor parameter for the grace bound.
- `tests/aprilcam/daemon/test_detection.py` — new/updated coverage for
  disappearance-grace behavior alongside existing appearance-streak
  tests.
- Daemon subsystem `DESIGN.md`, if the confirmation-gate behavior is
  documented there, updated to describe the grace mechanism.

### Out of Scope

- Detector parameter changes (`quad decimation`, decode sharpening,
  the five inert `aprilTag*` DetectorParameters, etc.) — the issue's
  own analysis proved the v1/v2 detectors identical; do not tune them.
- Optical-flow gap bridging (v1's approach to raising raw detection
  rate) — possible follow-up, not this sprint.
- View/client changes.
- Exposure or camera-config work (exposure was already ruled out as
  the cause and settled at `exposure=300` for this camera).

## Test Strategy

Unit-level tests against `DetectionEngine._confirm_detections` in
`tests/aprilcam/daemon/test_detection.py`:

- A tag confirmed once, then absent for bursts within the grace bound,
  continues to be reported each burst.
- A tag absent for more bursts than the grace bound resets and must
  satisfy the full `CONFIRMATION_STREAK` again before being reported.
- Existing appearance-streak tests (phantom-id suppression via
  `CONFIRMATION_STREAK`) continue to pass unmodified, confirming no
  regression to appearance-side behavior.
- Default grace value is asserted explicitly so the documented default
  is test-enforced, not just prose.

## Architecture

**Sizing: Compact.** This sprint adds disappearance-grace bookkeeping to
one existing module (`daemon/detection.py`'s
`DetectionEngine._confirm_detections`). It touches exactly one module,
introduces no new cross-module dependency (still only `camera/` and
`vision/`, as documented in the module's own docstring), changes no
dependency direction, and adds no new public data model — the new state
is internal engine bookkeeping (plain dicts/sets keyed by `TagId`), not
a new `TagRecord`/`TagFrame` field or wire type. The full 7-step
methodology and diagrams are not warranted; this section uses the
compact variant.

### What Changed

`daemon/detection.py` — `DetectionEngine._confirm_detections` and the
constructor's confirmation-parameter surface.

Today, `_confirm_detections` rebuilds `self._streaks[slug]` from
scratch every burst, keyed only by the tag ids present in *this*
burst's raw detections. A confirmed tag that misses even one burst
disappears from that dict entirely — its next real sighting starts a
brand-new appearance streak at 1 and must pay the full
`CONFIRMATION_STREAK` (3) consecutive-burst cost again before it is
reported at all. Combined with a marginal raw per-burst detection
probability (~0.45–0.6 for AprilTag 1 on `arducam-ov9281-usb-camera`),
this collapses the *reported* rate roughly to the raw rate cubed,
matching the issue's observed 1/12 polls.

The fix adds a disappearance-grace window **after** a tag has already
satisfied appearance confirmation. The engine now tracks three things
per camera slug, per tag id, instead of one:

1. `_streaks` (existing) — the appearance-building counter, now used
   only while a tag is *not yet* confirmed.
2. `_confirmed` (new) — the set of tag ids currently holding confirmed
   status for that slug.
3. `_grace_counters` (new) — for each currently-confirmed id, how many
   consecutive bursts it has now gone *without* being detected.

Per burst: an id detected this burst and not yet confirmed increments
its building streak, and is promoted into `_confirmed` once that
streak reaches `confirmation_streak`. An id detected this burst that
is already confirmed stays confirmed and has its grace counter reset
to 0. An id already confirmed but **not** detected this burst has its
grace counter incremented; it remains confirmed — silently absent from
just this burst's output — as long as the counter is at or below
`confirmation_grace`, and is dropped back to fully-unconfirmed (no
partial credit toward re-confirmation) the instant that bound is
exceeded. A `TagRecord` is only ever built for an id that is *both*
currently confirmed *and* actually present in this exact burst's raw
detections — a grace burst never fabricates, re-emits, or backdates a
record; it simply omits the tag from that one `TagFrame.tags`, exactly
as today's code omits any undetected id.

The constructor gains one new parameter, `confirmation_grace: int =
CONFIRMATION_GRACE`, mirroring `confirmation_streak`'s existing
injectable-with-documented-default pattern. `CONFIRMATION_GRACE` is a
new module constant with an evidence-style docstring (matching
`CONFIRMATION_STREAK`'s), default `8`.

### Why

The 3-burst appearance streak (`CONFIRMATION_STREAK`) exists on real
phantom-detection evidence (ticket 024-007) and must stay exactly as
it is — this sprint does not touch appearance. But
`_confirm_detections` never had a *disappearance* side: any single
miss, for any reason (a marginal raw decode, not a real disappearance),
pays the same cost as a brand-new tag first appearing. For a tag whose
raw detection probability is well below 1 — which the issue's
root-cause analysis established is a real property of AprilTag 36h11
on this camera, not a detector-parameter bug — that means the
confirmation gate itself, not the detector, is what makes the tag
"effectively invisible." Grace closes that gap without reopening the
phantom-suppression hole: an id still needs `confirmation_streak`
genuine, strictly-consecutive hits to ever become confirmed in the
first place; grace only changes what happens to an *already-earned*
confirmed status across a later gap.

### Impact on Existing Components

None — additive. `run_burst`'s call site, `_build_tag_record`,
`_apply_mount_correction`, `_finish_record`, and the ring buffer are
all untouched; every existing appearance-streak test continues to
exercise the unchanged building-phase path (a tag that never reaches
`confirmation_streak` consecutive hits never enters `_confirmed` and
is therefore never eligible for grace at all).

### Design Rationale

**Decision 1 — grace unit is a missed-burst count, not wall-clock
time.** Context: `run_burst` has no fixed cadence — a `get_tags`-only
consumer bursts once per poll (arbitrary human/robot-controlled
interval), while a tag-stream subscriber drives bursts continuously at
`TAG_STREAM_INTERVAL_S` (~30 Hz). Alternatives considered: a
wall-clock grace horizon (e.g. "keep confirmed for up to 2 seconds
since last detection"). Why burst-count wins: `confirmation_streak`
itself is already a burst count, not a time bound, and this module has
no clock dependency anywhere else in its confirmation logic — a
time-based grace would need to plumb a clock/interval concept into a
component that is explicitly demand-driven with no assumed frame rate
(module docstring). A burst-count grace stays symmetric with the
existing mechanism and needs no new concept. Consequence: at stream
cadence, `confirmation_grace=8` bursts is a fraction of a second; at
occasional-poll cadence, it is up to 8 polls — the same bound reads
very differently in wall-clock terms depending on who is calling, and
callers relying on prompt disappearance detection at low poll rates
should poll more often or use a smaller override, not expect a fixed
wall-clock guarantee.

**Decision 2 — default `CONFIRMATION_GRACE = 8`.** Context: needed a
default that meaningfully bridges gaps for the documented worst case
(AprilTag 1's raw rate ~0.45–0.6, i.e. per-burst miss probability
~0.4–0.55) without unreasonably delaying detection of a genuine
disappearance. Reasoning: treating consecutive misses as independent,
`P(≥N consecutive misses while the tag is still genuinely present) ≈
q^N` where `q` is the per-burst miss probability. At the documented
worst case `q≈0.55`, `N=8` gives `q^8 ≈ 1.1%`; at a more typical `q≈0.5`
it is `q^8 ≈ 0.4%` — both small enough that a marginal-but-present tag
almost never gets bounced back to full re-confirmation by chance,
while a genuinely-removed tag still leaves confirmed status within a
handful of bursts (fractions of a second at stream cadence). This
mirrors `CONFIRMATION_STREAK`'s own evidence-based-but-explicitly-
revisable framing: `CONFIRMATION_GRACE`'s docstring documents this
reasoning and its dependence on assumed `q`, and the parameter stays
constructor-injectable specifically because a longer real-hardware
campaign could motivate a different value, exactly as
`CONFIRMATION_STREAK`'s own docstring anticipates for itself.
Alternatives considered: values at the low end (5) trade a slightly
higher false-reset rate for slightly faster genuine-disappearance
detection; values at the high end (15) do the reverse. 8 sits in the
middle of the sprint's own suggested 5–15 range and is the smallest
value that gets the documented worst case comfortably under 2%.

**Decision 3 — mechanism is streak-preservation-only, not
record re-emission.** Context: during a grace burst, should the
engine (a) simply omit the tag from that burst's output while
preserving confirmed status internally, or (b) re-emit the tag's
last-known `TagRecord` (optionally keeping its original, now-stale,
timestamp) so the tag still shows up in every burst? Alternatives
considered: (b) full re-emission with a refreshed timestamp — rejected
outright, since it would silently report a stale position as current,
which is exactly the "silently wrong is worse than absent" failure
mode this codebase avoids elsewhere (`daemon/DESIGN.md`'s deskew
guard is the same principle). (b') re-emission preserving the
*original* detection's timestamp, so a consumer can compute staleness
from `TagRecord.timestamp` itself — more honest than (b), but it
still injects a synthetic entry into the ring buffer for a burst in
which nothing was actually detected, which pollutes
`_history_for_tag`'s world-position history with a duplicate sample at
an old timestamp and complicates `compute_motion`'s finite-difference
assumptions for no benefit this sprint asks for. Why (a) wins: with
streak-preservation-only, the *reported* rate across many polls
converges to the tag's true raw detection rate (~0.5 in the documented
case) rather than collapsing to that rate cubed (~0.1–0.2, matching
the issue's observed 1/12) — a roughly 3–5x improvement using only
genuinely-observed detections, with zero risk of a stale position
reaching a consumer that might be planning against a moving robot.
This does not reach a ~1.0 reported rate the way re-emission would,
but it is the honest ceiling: every reported record is a real,
current-burst detection, timestamps never lie about when a position
was actually seen, and the ring buffer never gains a fabricated or
duplicated entry. Consequence: a consumer that wants a smoothed
"last-known position" view still needs its own staleness-aware logic
on top of `get_tag_history`; this sprint does not build that, and
`sprint.md`'s Out of Scope already excludes optical-flow-style gap
bridging as a separate follow-up.

### Migration Concerns

None. No persisted state, no wire/proto change, no data-model change.
`confirmation_grace` is a new constructor parameter with a default —
every existing caller/test that constructs a `DetectionEngine` without
passing it keeps exactly today's re-confirmation-on-any-miss behavior
for as long as `CONFIRMATION_GRACE`'s default did not exist... except
that the default *does* now apply grace by default. This is a
deliberate, documented behavior change to the default construction
path (matching how `CONFIRMATION_STREAK` was itself raised from 2 to 3
by ticket 024-007 without a compatibility flag) — no existing test
exercises "a confirmed tag misses exactly one burst and must
immediately re-earn confirmation" (verified by reading
`tests/aprilcam/daemon/test_detection.py` in full), so no currently-
passing assertion is contradicted by this default changing.

## Use Cases

**Sizing: Compact** — one new sprint-level use case, briefly stated,
covering the reliability improvement to the existing tag-query flow.

### SUC-001: Repeated tag queries stay reliable for a marginal-but-present tag
Parent: UC-10 (also serves UC-09, which shares the same confirmation
gate for a single-tag query)

- **Actor**: A robot client program (A-02) or CLI/MCP caller (A-01)
  repeatedly querying a camera's detected tags (`get_tags`/`get_tag`,
  one-shot or via a tag stream).
- **Preconditions**: A tag has already satisfied appearance
  confirmation (`CONFIRMATION_STREAK` consecutive genuine hits) and
  has a raw per-burst detection probability well below 1.0 (e.g. the
  documented AprilTag-1-on-`arducam-ov9281-usb-camera` case, ~0.45–0.6).
- **Main Flow**:
  1. Client repeatedly calls `get_tags`/`get_tag`, or holds a tag
     stream open.
  2. On a burst where the tag is genuinely detected, it is reported —
     unchanged from today.
  3. On a burst where the tag is momentarily not detected, but the
     miss count is within `confirmation_grace`, the daemon keeps the
     tag's confirmed status alive internally; that one burst's
     `TagFrame` honestly omits the tag rather than reporting a
     fabricated or stale position.
  4. When the tag is redetected within the grace window, it is
     reported immediately — no re-payment of `CONFIRMATION_STREAK`.
  5. If the tag goes more than `confirmation_grace` consecutive bursts
     without a genuine detection, its confirmed status is dropped; the
     next sighting starts a fresh appearance streak, exactly as today.
- **Postconditions**: Across many polls, the tag's reported-presence
  rate tracks its true raw detection rate rather than collapsing to
  that rate cubed. A genuinely-removed tag still disappears from
  confirmed status within `confirmation_grace` bursts.
- **Acceptance Criteria**:
  - [ ] A tag confirmed once, then missing for ≤ `confirmation_grace`
        consecutive bursts, is reported immediately on its next
        genuine detection — no re-confirmation delay.
  - [ ] A tag missing for > `confirmation_grace` consecutive bursts is
        dropped and must satisfy the full `CONFIRMATION_STREAK` again
        before being reported.
  - [ ] No burst ever reports a tag that was not actually detected in
        that exact burst — no fabricated or re-emitted records.
  - [ ] Appearance-side behavior is unchanged: an id that never
        reaches `CONFIRMATION_STREAK` consecutive hits (including one
        that gets only 1–2 hits before a gap) is never confirmed and
        never becomes eligible for grace.
  - [ ] `CONFIRMATION_GRACE`'s documented default (8) is asserted
        directly, the same way `CONFIRMATION_STREAK`'s default is
        pinned today.

## GitHub Issues

(GitHub issues linked to this sprint's tickets. Format: `owner/repo#N`.)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [ ] Sprint planning document is complete (sprint.md, including its
      Architecture and Use Cases sections)
- [ ] Architecture review passed (or skipped, for changes with no
      architectural impact)
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | Add disappearance-grace to DetectionEngine confirmation gate | — |

Tickets execute serially in the order listed.
