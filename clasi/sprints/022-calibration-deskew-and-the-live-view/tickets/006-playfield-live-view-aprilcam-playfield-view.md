---
id: '006'
title: 'Playfield live view: aprilcam playfield view'
status: done
use-cases:
- SUC-006
depends-on:
- '002'
- '005'
github-issue: ''
issue: calibration-deskew-and-the-live-view.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Playfield live view: aprilcam playfield view

## Description

Implements SUC-006 (parent UC-18): `aprilcam playfield view` — the
deskewed, calibrated, tags-drawn live view, reusing `view/viewer.py`'s
poll-driven core from ticket 002 and `client/renderer.py` from ticket
005. Per Architecture Decision 3, the poll loop calls `get_tags`
(triggering a fresh detection burst — `get_frame` alone never does,
per sprint 021's Decision 4) immediately before `get_frame(
deskewed=True)` each tick, at the same ~2 Hz starting rate as ticket
002's raw view.

Builds/modifies:

- `view/viewer.py` extended — a deskewed+tags mode: poll
  `get_tags(camera)` then `get_frame(camera, deskewed=True)`, pass the
  returned `ImageFrame` through `client/renderer.py`'s
  `Renderer.render()`, display the rendered PNG. Uncalibrated-camera
  path: the daemon's `NotCalibrated` on the first `get_frame(
  deskewed=True)` call surfaces as an explicit CLI error naming
  calibration as the missing step — the viewer must **not** catch this
  and silently fall back to a raw frame (UC-18.S-2's central rule).
- `cli/playfield.py` extended — `aprilcam playfield view`, refuses
  `--all`.

**Archive pointers**: none beyond what tickets 002/005 already
established — this ticket is composition of already-built pieces, not
new geometry or rendering logic.

## Acceptance Criteria

- [x] `aprilcam playfield view` on a calibrated camera shows the
      deskewed field, north up, east right, with AprilTag 1 drawn at
      the image centre (UC-18.S-1).
- [x] **A second tag's drawn position moves right when the physical
      tag moves east, and up when it moves north** — the mirror-
      catching check, now verified end-to-end through the full
      poll→render→display path (the same physical motions SUC-004
      verifies numerically and SUC-005 verifies for a single static
      frame; this is the live, continuously-updating version).
- [x] An **uncalibrated** camera's `playfield view` fails explicitly,
      naming calibration as the missing step, and **never** displays a
      raw frame as a fallback — verified by asserting the viewer makes
      zero raw-frame requests on this path (UC-18.S-2).
- [x] Opening `playfield view` causes the target camera's ring buffer
      to begin filling at approximately the poll rate for as long as
      the window stays open (the documented consequence of
      Architecture Decision 3 — sprint 021's Open Question 3 becomes
      true only while a poll-based consumer like this is active);
      closing the window stops new bursts.
- [x] `aprilcam playfield view --all` is refused (single-target,
      matching tickets 002/003's refusals).
- [x] If calibration goes stale mid-view (`TagFrame.
      calibration_stale=True` arrives on a poll), this is surfaced in
      the view rather than silently ignored (UC-18's stale-mid-view
      failure mode) — a visible indicator is sufficient; this does not
      require stopping the view.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/view/
  tests/aprilcam/client/test_daemon_client.py`.
- **New tests to write**: `tests/aprilcam/view/test_viewer.py`
  additions — deskewed-mode poll sequencing (asserting `get_tags` is
  called before each `get_frame(deskewed=True)`), the `NotCalibrated`
  no-fallback assertion, the `calibration_stale` surfacing check — all
  against a fake/stub client, no real daemon or GUI toolkit.
- **Verification command**: `uv run pytest && uv run mypy` clean; zero
  `typing.Protocol`; baseline 388 passing, additive only.
