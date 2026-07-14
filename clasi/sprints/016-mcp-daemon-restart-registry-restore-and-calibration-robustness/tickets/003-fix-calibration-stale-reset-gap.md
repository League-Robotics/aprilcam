---
id: '003'
title: Fix calibration_stale reset gap
status: in-progress
use-cases:
- SUC-003
depends-on: []
github-issue: ''
issue: mcp-playfield-registry-not-restored-after-daemon-restart.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Fix calibration_stale reset gap

## Description

The issue reported `info.json`'s `calibration_stale: true` firing from a
~3px corner-marker shift, which read initially as "the staleness threshold
is too strict." Investigation during sprint planning found this diagnosis
is not supported by the evidence: the live-boundary geometric check in
`src/aprilcam/core/playfield.py` (`_update_static`, threshold from
`_effective_movement_threshold()`) uses a default
`DEFAULT_MOVEMENT_THRESHOLD_PX` of **25.0px**
(`src/aprilcam/calibration/geometry.py:58`) — a 3px shift is well under
that default and should not, by itself, trip the flag.

Instead, code inspection found `_calibration_stale` is **set** to `True`
when a marker's live position exceeds the threshold (`playfield.py` ~line
346), but **no code path resets it back to `False`** once the marker's live
position returns within threshold. This is a genuine, code-confirmed defect
independent of threshold tuning: if the flag was set by an earlier, larger
excursion (e.g. the marker-position swing that can occur during the camera
reopen/reconnect transient that accompanies a daemon restart — the same
restart episode reported in the source issue), it persists indefinitely,
and the ~3px figure observed at inspection time is unrelated to whatever
originally tripped the flag.

This ticket fixes the confirmed reset gap. It explicitly does **not**
change the 25px threshold value — see `architecture-update.md`'s Design
Rationale ("fix the `_calibration_stale` reset gap; do not adjust the 25px
threshold") for why loosening an unconfirmed-strict threshold risks
defeating the check's actual purpose (Sprint 011's explicit goal: "never
silently deskew with a stale transform after a bump").

## Acceptance Criteria

- [ ] `src/aprilcam/core/playfield.py`: `_update_static` (or the boundary's
      update cycle) gains a symmetric reset — when all tracked static
      markers are within `_effective_movement_threshold()` on a given
      check, `_calibration_stale` is cleared back to `False`, mirroring the
      existing set-to-`True` path.
- [ ] The reset is symmetric and correct in both directions: a marker that
      exceeds threshold sets the flag `True`; on a subsequent check where
      all tracked markers are back within threshold, the flag clears to
      `False`. Partial recovery (some markers within threshold, at least
      one still outside) keeps the flag `True`.
- [ ] `src/aprilcam/daemon/camera_pipeline.py`'s `_maybe_update_stale_flag()`
      requires no changes — it already writes `info.json` only "when the
      flag changes" and already treats `True→False` and `False→True`
      symmetrically; confirm this by running it against the new reset logic
      rather than modifying it speculatively.
- [ ] **No threshold value change.** `DEFAULT_MOVEMENT_THRESHOLD_PX` (25.0px,
      `calibration/geometry.py:58`) and any per-camera
      `movement_threshold_px` override remain unchanged in this ticket.
- [ ] The flag remains purely advisory after this fix — confirm no new
      gating is introduced anywhere that reads `calibration_stale`
      (`mcp_server.py:685-686` surfaces it informationally on
      `open_camera`'s response; `grpc_server.py` has zero references per
      planning-time grep — both should remain true after this change).
- [ ] Regression test on `PlayfieldBoundary`/`_update_static` directly:
      simulate marker positions exceeding threshold then returning within
      threshold across successive `update()` calls; assert
      `calibration_stale` transitions `False → True → False`.
- [ ] `uv run pytest` green; no regression in existing playfield/calibration
      tests.

## Testing

- **Existing tests to run**: any existing tests covering
  `core/playfield.py` (`PlayfieldBoundary`, static-marker tracking) —
  grep `tests/` for `playfield` and `calibration_stale`;
  `tests/test_daemon_server.py` if it exercises
  `_maybe_update_stale_flag`.
- **New tests to write**:
  - `False → True → False` transition test on `PlayfieldBoundary` /
    `_update_static` (the core regression test for this ticket, described
    above under Acceptance Criteria).
  - Partial-recovery test: only some tracked markers return within
    threshold — assert the flag stays `True` until all do.
  - Confirm `_maybe_update_stale_flag()` writes `info.json` on both the
    `False→True` and `True→False` transitions (it should already, per
    existing "write on change" logic — this test guards against a future
    regression, not a current bug).
- **Verification command**: `uv run pytest`

## Implementation Plan

**Approach**: add the missing reset branch alongside the existing set
branch in `_update_static`, in the same function, same file. No interface
change, no new module, no threshold change.

**Files to create/modify**:
- `src/aprilcam/core/playfield.py` — add the `False`-transition reset logic
  to `_update_static` (or equivalent update-cycle method).
- `tests/test_playfield_calibration_stale_reset.py` (new, or added to an
  existing `core/playfield.py`-focused test file if one exists) — the
  transition tests described above.

**Testing plan**: unit tests directly against `PlayfieldBoundary` with
synthetic marker-position sequences — no live camera or hardware required.

**Documentation updates**: none required; the flag's advisory, resettable
behavior is now correctly implemented rather than newly introduced, so no
external-facing documentation changes are needed.
