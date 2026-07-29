---
status: pending
---

# Calibration executes inside the daemon, triggered by RPC

Per [specification.md](../../docs/design/specification.md) §4.4: today
`aprilcam calibrate` (calibrate_cli.py) and the MCP `calibrate_playfield`
tool run the calibration math locally — importing cv2 and pulling frames
over gRPC through a `_DaemonCapture` shim. This breaks the vision
boundary (daemon = sole vision authority) and is why the MCP server
still needs OpenCV for some tools.

## Scope

- New daemon RPC (e.g. `Calibrate(cam_name, options) → progress/result`)
  that runs `calibrate_from_playfield_def` inside the daemon, on frames
  it already owns, then hot-reloads the pipeline homography (existing
  `ReloadCalibration` machinery).
- `aprilcam calibrate` and MCP `calibrate_playfield` become pure
  orchestrators: trigger the RPC, report progress and results. No cv2,
  no frame access.
- All calibration invariants preserved: ArUco tid `-(N+1)` mapping,
  A1-centred +y-north world frame, `calibration.json` written before
  `config.json` with config failures swallowed, config-owned key
  overlay, static-marker recording.
- Fix in passing: `CameraPosition` docstring says "origin is the
  playfield center" while `correct_world_for_height` is A1-centred —
  align the wording.
- The `--joint` CLI path is superseded; see
  [[fix-calibrate-joint-nameerror]] — if this issue lands first, the
  joint path is removed rather than fixed.

## Acceptance sketch

On a machine with only the base install (no cv2), `aprilcam calibrate`
completes a calibration end-to-end against a daemon host and the daemon
serves updated world coordinates without restart.
