---
status: done
sprint: '033'
tickets:
- 033-001
---

# Playfield overlay and sidebar should show AprilTags only — stop drawing over ArUco markers

## Description

Stakeholder request (2026-08-22 session, verbatim intent): "You don't
have to display the ArUco tags. You only have to display the April
tags. We don't need to draw over the ArUco tags."

The ArUco 4x4 markers are the playfield's own calibration fixtures —
ten of them ring the field. Drawing the red-U/green-hat/ID/crosshair
overlay (sprint 032) over every one of them buries the field in
annotation noise and adds nothing: they never move and their positions
are calibration inputs, not game state.

## Change

- `client/renderer.py`: in the per-record tag-drawing loop, skip
  records whose `record.tag` is not an AprilTag family — draw the
  overlay (U, hat, ID, crosshair) only for AprilTag records. Verify
  the actual family value(s) on `TagId` (`types.py`) rather than
  assuming a string — check what the daemon's detection emits for
  AprilTag 36h11 vs ArUco 4x4 records.
- `view/viewer.py` sidebar: same filter — list AprilTag records only.
  (The sprint-032 sidebar formats whatever `DisplayFrame.tags`
  carries; either filter at the converter when populating
  `DisplayFrame.tags`... note that would also hide ArUco from any
  future consumer of that field — or filter in the sidebar/renderer
  consumers. Planner picks the seam; the requirement is only that
  neither the overlay nor the sidebar shows ArUco records.)
- Annotations, deskew, and detection are untouched — ArUco detection
  must keep running in the daemon (calibration depends on it); this is
  presentation-side filtering only.

## Constraints

- cv2-free client rule still applies (Pillow only).
- Preserve the single-subscription contract
  (`test_run_playfield_view_never_calls_get_tags_only_stream_images`).
- View-only: no daemon/RPC/wire changes.
