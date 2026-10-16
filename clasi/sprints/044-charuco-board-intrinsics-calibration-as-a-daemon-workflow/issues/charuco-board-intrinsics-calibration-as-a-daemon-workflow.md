---
status: in-progress
sprint: '044'
tickets:
- 044-001
- 044-002
- 044-003
- 044-004
- 044-005
- 044-006
- 044-007
- 044-008
- 044-009
- 044-010
- 044-011
- 044-012
---

# ChArUco-board intrinsics calibration as a daemon workflow, with a fullscreen guided GUI

## Description

Today the only way to get a lens model (`camera_matrix`/`dist_coeffs`)
into a camera's calibration is `calibrate --with-intrinsics`, a
single-view fit over the playfield's surveyed markers. It is structurally
degenerate (one coplanar view; see the module docstring in
`calibration/calibration.py`), crashes at six points
(`estimate-intrinsics-crashes-with-exactly-six-ground-truth-points.md`),
and on camera 4 produced fx = 473 where the true value is 911. On
2026-10-15 the stakeholder bought ChArUco boards and we calibrated
cameras 2, 3 and 4 properly with an out-of-process prototype
(`docs/knowledge/assets/charuco-prototype/`, lessons in
`docs/knowledge/2026-10-15-charuco-intrinsics-calibration-lessons.md`).
That prototype runs OpenCV in a client process and writes the daemon's
`calibration.json` behind its back, which the daemon only re-reads on
restart. It needs to become a real AprilCam feature.

## What to build

**Daemon (the sole vision authority) owns the whole vision side.**

- An *intrinsics session* per camera: `start_intrinsics(camera, board)`,
  `get_intrinsics_status(camera)`, `finish_intrinsics(camera, install)`,
  `abort_intrinsics(camera)`. While a session is open the daemon runs
  ChArUco detection on that camera's frames and keeps *views*: distinct
  (novelty by corner displacement), steady (motion gate vs previous
  frame), sharp (blur relative to the camera's running best), with a
  confident pose (IPPE two-solution ambiguity check). See the knowledge
  entry for every constant we earned; the marker-clustering trick is
  required for boards sharing the field's dictionary, and detection runs
  on a 2x upscale with loosened candidate filters because the markers are
  at the detector's limit at table height.
- Periodic background solve (`cv2.calibrateCamera`, k3 fixed) with the
  **good-enough metric**: max residual, after a homography, between the
  undistortion map of the full solve and of random half-sample solves,
  over the central ~85 % of the frame. Status reports: view count,
  tilted count, 3x3 region coverage (by board extent, outer 40 % bands),
  tilt-direction coverage (N/E/S/W), rms px, the residual, the size of the
  correction itself vs no lens model, and a plain-language *next* hint
  derived from the biggest coverage gap. Good = rms < 0.6 px and
  residual < 4 px (~3 mm).
- **Install without a restart.** `finish_intrinsics(install=True)` stores
  the lens model so that the running daemon uses it immediately and the
  next field `calibrate` carries it forward. Decide where it lives:
  today `camera_matrix`/`dist_coeffs` ride inside `calibration.json`,
  which is per-playfield-calibration state; a lens model is per camera
  (and per resolution) and should survive playfield changes and failed
  field calibrations. A separate `intrinsics.json` per camera, loaded at
  start-up and consulted by `Calibrator` via `get_prior_calibration` (or
  a sibling), is the obvious shape; record provenance (board spec, view
  count, rms, residual, timestamp, `measured` vs the old `estimated`).
- Board spec is a config value with a default
  (`5x7:37:28:DICT_5X5_100`, i.e. COLSxROWS:SQUARE_MM:MARKER_MM:DICT) and
  auto-detects OpenCV legacy vs current layout.
- Every session is fresh; nothing carries over between runs unless
  explicitly resumed.

**Clients stay thin (no OpenCV).**

- `aprilcam camera intrinsics <camera...> [--board SPEC] [--install]`:
  the fullscreen Tk GUI the stakeholder accepted on 2026-10-15, driven
  purely by `get_intrinsics_status` polling: banner (SHOW ME THE BOARD /
  KEEP GOING / GOOD ENOUGH), per-camera "error now" and "could still
  change by" in very large type (fonts scaled from screen height, banner
  about H/9 -- readable from 10 feet), 3x3 region tiles and tilt tiles,
  one NEXT line. **Never auto-quit on green**: Enter finishes and
  installs, Esc aborts. A `--no-gui` text mode prints the same status
  lines for SSH use. Multiple cameras at once (2 and 4 both see the main
  field).
- MCP tools mirroring the four daemon commands so an agent can run the
  workflow while a human holds the board.
- `aprilcam camera calibrate` stays as the field-homography step; its
  output should say which lens model it carried forward (measured /
  estimated / none). `--with-intrinsics` becomes the documented fallback
  for when no board is available.

## Why

The playfield now lives on a portable table and gets moved often. With a
measured lens model the frequent step is the seconds-long field
`calibrate`; the board routine is once per camera, redone only on a
lens/focus/resolution change. Camera 3 has ~110 px of barrel distortion
at the corners, which is why its four-corner homography never fit the
mid-edge markers; cameras 2 and 4 turned out nearly rectilinear, which
the old estimate could not have told us.

## Acceptance

- From a cold daemon, `aprilcam camera intrinsics 3` reaches GOOD ENOUGH
  in under a minute of board motion on the secondary field, Enter installs,
  and an immediate `aprilcam camera calibrate 3` reports the measured
  model carried forward -- no daemon restart anywhere.
- `get_calibration` / `camera list` expose whether a camera's lens model
  is measured, estimated, or absent.
- Hardware-free tests cover: marker clustering against field-marker
  collisions and two boards; the legacy/current layout auto-detect;
  pose-confidence rejection of a flat board; relative blur and motion
  gates; coverage by board extent; the good-enough residual on synthetic
  solves; fresh-session semantics.

## Related

- `docs/clasi/todo/capture-worker-dies-when-daemon-stdin-tty-is-revoked.md`
  (separate; hit the same day).
- `estimate-intrinsics-crashes-with-exactly-six-ground-truth-points.md`
  becomes moot for the main path but the clean refusal is still wanted.
