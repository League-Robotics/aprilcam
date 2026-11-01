---
status: pending
---

# `camera probe` rows show "uncalibrated" and "lens=absent" for cameras `camera list` shows calibrated

## Description

Seen on buzzkill and donald, 2026-11-01: `aprilcam -d HOST camera probe`
printed `playfield=-  uncalibrated  flatfield=absent  lens=absent` for
cameras that `camera list` on the same daemon, moments later, showed as
`playfield=main-playfield  calibrated (pose, full)  flatfield=present
lens=measured`. The probe result's per-camera state is evidently built
without the calibration / playfield / flat-field / lens fields that
`list_cameras` fills in, and the CLI renders the two with the same row
formatter.

### Needed

- `ProbeCameras` rows carry the same calibration, playfield, flat-field
  and lens fields as `ListCameras` (or the CLI renders probe rows without
  those columns rather than printing false defaults).
