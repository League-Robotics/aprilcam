---
id: '004'
title: Docs and live-check note for the image-based lens GUI
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
depends-on:
- '003'
github-issue: ''
issue: intrinsics-gui-shows-the-live-camera-image-with-a-labelled-coverage-grid.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Docs and live-check note for the image-based lens GUI

## Description

Document the new behaviour and write the short live check the stakeholder runs,
including the acceptance-rate comparison with and without the image.

## Implementation Plan

- `src/aprilcam/view/DESIGN.md`: image-per-column design, overlay composition as
  a pure function, frame polling cadence, dimmed-on-failure, no-cv2 rule.
- `src/aprilcam/intrinsics/DESIGN.md`: `board_outline` on the status record
  (hull per board, native pixels, cleared when no board).
- `docs/knowledge/2026-10-16-charuco-intrinsics-live-acceptance-runbook.md`
  (append a section; or a new dated knowledge file if it reads better): live
  check steps -- run `aprilcam camera lens 2 4`; confirm two columns with live
  images, labelled grid, board outline following the board, cells going green
  when the tile would; count kept views per minute with the image on versus a
  baseline run of the same board motion without it (e.g. `--no-gui` text mode
  or the previous build) and record both numbers; unplug/replug or stop the
  daemon to see the dimmed image plus banner. Note the remote-daemon bandwidth
  caveat.
- README: one line under the `camera lens` description if one exists.

## Acceptance Criteria

- [x] Both DESIGN.md files describe the new behaviour accurately.
- [x] Runbook/knowledge note contains the live-check steps and the rate
      comparison procedure with a place to record results.
- [x] Docs only; no code changes.

## Testing

- **Existing tests to run**: none required (docs only); run `uv run pytest tests/aprilcam/view -q` as a sanity check
- **New tests to write**: none
- **Verification command**: `uv run pytest tests/aprilcam/view -q`
