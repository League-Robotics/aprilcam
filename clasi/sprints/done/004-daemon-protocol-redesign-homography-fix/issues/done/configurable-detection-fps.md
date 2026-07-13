---
status: done
sprint: '004'
tickets:
- 004-011
---

# Configurable detection loop frame rate with per-camera override

Add a `detection_fps` setting that caps how fast the detection pipeline captures frames and runs AprilTag detection.

## Configuration sources (lowest → highest priority)

1. **Environment / `.env`**: `APRILCAM_DETECTION_FPS=10` — the system-wide default loaded by `Config`.
2. **`calibration.json`** per-camera field `"detection_fps"` — overrides the env default for that specific camera.

If neither is set, fall back to a hard-coded default of `10`.

## Behaviour

- The detection loop (in `src/aprilcam/daemon/camera_pipeline.py`) sleeps after each frame to enforce the target interval (`1 / detection_fps` seconds). It uses the actual frame timestamp to account for processing time so drift doesn't accumulate.
- The setting is read at pipeline startup. Changing it requires restarting the camera (no hot-reload needed).

## Calibration flow

When `aprilcam calibrate` runs for a camera:
- Read `APRILCAM_DETECTION_FPS` from the environment (or `Config`) as the default.
- If the existing `calibration.json` already has `"detection_fps"`, keep it unchanged.
- Otherwise, write the env default into `calibration.json` so the per-camera file is always self-contained.

## Scope

- `src/aprilcam/config.py` — add `detection_fps: int` field, read from `APRILCAM_DETECTION_FPS`.
- `src/aprilcam/daemon/camera_pipeline.py` — honour `detection_fps` in the frame loop; sleep to enforce rate.
- `src/aprilcam/calibration/calibration.py` (or the save helper) — persist `detection_fps` into `calibration.json`.
- `src/aprilcam/core/playfield.py` — pass `detection_fps` through to the pipeline when constructing from a calibration file.
- `.env.example` (or equivalent) — document `APRILCAM_DETECTION_FPS=10`.
