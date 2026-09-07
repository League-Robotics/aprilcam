---
status: pending
---

# Raw `camera view` should offer an optional tag overlay for uncalibrated cameras

## Description

Stakeholder use case, 2026-09-06: watching tags rotate under a camera
that has no playfield. `camera view` is deliberately raw (UC-16, "raw
means raw") and `playfield view` requires calibration, so there is no
live picture with tag outlines and facing hats for an uncalibrated
camera. `TagRecord.pixel_corners` exists precisely so a client can
draw on the raw stream without vision processing.

### Needed

- `aprilcam camera view <camera> --tags` subscribes to `stream_tags`
  alongside the raw image stream and draws each tag's pixel quad, id
  and front-edge hat. Default stays raw; the window title must say
  `[RAW+TAGS]` so UC-16's diagnostic guarantee is not confused.
- Lower priority than
  `camera-tags-needs-a-follow-mode-that-streams-detections.md`.
