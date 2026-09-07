---
status: pending
---

# `camera tags` needs a `--follow` mode that streams detections instead of one burst per call

## Description

Stakeholder use case, 2026-09-06: cameras with no playfield, looking
down at AprilTags, to record tag rotation over time. Today the only
CLI paths are `camera tags` (one detection burst) and `camera view`
(raw, no overlay); `playfield view` requires calibration. The daemon
already has a per-camera `TagStreamProducer` (`stream_tags`) but no
CLI consumes it.

A one-shot burst also interacts badly with the temporal confirmation
filter in `detection.py` (`_confirm_detections`, streak of 3): the
first `camera tags` after any gap prints `(no tags detected)` even
with four tags plainly in view, and only repeated calls surface them.
A continuous subscriber is the mode that filter was designed for.

### Needed

- `aprilcam camera tags <camera> --follow` subscribes to
  `stream_tags` and prints one row per tag per frame (or per change)
  until interrupted, with timestamps, pixel position and the
  image-space yaw from
  `uncalibrated-tag-records-should-report-image-space-yaw-in-degrees.md`.
- Optional `--json` for machine-readable recording.
- Consider a `--bursts N` or note in `camera tags` help that the
  confirmation filter needs consecutive bursts.
