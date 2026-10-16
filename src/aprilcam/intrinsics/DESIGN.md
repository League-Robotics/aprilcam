# intrinsics/

**Owner:** Eric Busboom · **Last reviewed:** 2026-10-15 · **Status:** stable (sprint 044)

---

## 1. Purpose

`intrinsics/` measures a camera's **lens** (camera matrix and distortion coefficients) from a printed ChArUco board, as a daemon workflow: the daemon reads frames from the camera it already owns, finds boards, keeps only trustworthy views, solves continuously, and installs the result with no daemon restart. It replaces the out-of-process prototype (`docs/knowledge/assets/charuco-prototype/`), which ran OpenCV in a client process and violated the vision boundary (`design.md` §2.1).

The boundary with `calibration/`: this package *produces* a lens model; `calibration/lens.py` (`LensModel`, `LensStore`) owns how it is persisted and how a later field calibration carries it forward. This package never writes a file itself — installation goes through `LensStore`.

## 2. Orientation

- **`interfaces.py`** — `cv2`-free value types and errors, safe for `client/`, `mcp/`, `view/`, `cli/` to import: `BoardSpec` and `resolve_board_spec` (request > env `APRILCAM_CHARUCO_BOARD` > default `5x7:37:28:DICT_5X5_100`, i.e. `COLSxROWS:SQUARE_MM:MARKER_MM:DICT`), `BoardLayout`, `BoardDetection`, `RejectReason`, `ViewVerdict`, `KeptView`, `CoverageSummary`, `LensSolution`, `LensEvaluation`, `IntrinsicsStatus`, `IntrinsicsFinishResult`, `derive_banner`, and the typed errors.
- **`detector.py`** — multi-board ChArUco detector (imports `cv2`). Markers detected on a 2x upscale with loosened candidate filters, clustered per physical board, corners interpolated on the native image. Board layout (normal/mirrored) is auto-detected and locked per instance.
- **`collector.py`** — `ViewCollector`: decides which detections become kept views — minimum corners, IPPE pose with an ambiguity ratio gate, frame-to-frame motion gate, blur gate relative to the camera's best sharpness, and a novelty/coverage rule (shift, tilt delta, nine frame regions, edge emphasis). Thresholds are named constants.
- **`solver.py`** — `LensSolver`: solves on all views and on random halves, undistorts a grid over the central ~85% of the field, absorbs projective differences with a homography (a planar field would), and takes the worst disagreement as the **good-enough residual** (`GOOD_RESIDUAL_PX = 4.0`, RMS gate `0.6`). Also produces the plain-language **next hint** (which region, which tilt).
- **`session.py`** — one live session on one camera: a capture thread (`Camera.read()` at ~8 fps) and a solver thread (re-solve every 3 new views), publishing one immutable `IntrinsicsStatus` snapshot under a lock. Holds corner arrays only, never frames. Time/thread dependent parts are injectable; `step()`/`maybe_solve()` are driven synchronously in tests.
- **`target.py`** — `ChArUcoTargetObserver` (sprint 045): a callable `(gray, CalibrationTarget) -> [TargetCorner(field_cm, pixel)]` that runs `BoardDetector` and maps each ChArUco corner id (via `collector.object_points`, board metres -> cm) through the declared placement: board centre, rotation (counter-clockwise from above; 0 = upright, top edge north) and the single board-y-down -> field-y-north flip (`board_to_field_cm`, pinned by test). `DaemonService` constructs it and injects it into `calibration.Calibrator` as `observe_target`; `calibration/` never imports this package (sprint 045 Decision 6, enforced by an import-graph test). Nothing about the board is persisted.
- **`manager.py`** — `IntrinsicsManager`: at most one session per camera slug, a 120 s no-status-poll auto-abort watchdog, abort-everything on shutdown. The only thing `DaemonService` talks to; it knows nothing about cameras or stores (the service injects a frame-source factory and an install callback).

## 3. Constraints and Invariants

- `interfaces.py` imports no `cv2`; everything else here is daemon-only.
- A session never holds frames, only per-view corner arrays.
- `get_intrinsics_status` never blocks on a solve (snapshot swap).
- **No auto-finish.** A session never ends itself when it turns green; only an explicit finish/abort, the idle watchdog, or daemon shutdown ends it.
- `finish` runs a final full solve and installs only when the result is good or `force` is set; the message says the lens is installed and `calibrate` must be run to apply it (`RUN_CALIBRATE_NOTE`).
- The constants in `collector.py`, `solver.py`, `session.py`, `manager.py` are fixed, not request parameters; their rationale is `docs/knowledge/2026-10-15-charuco-intrinsics-calibration-lessons.md`.

## 4. Design

Decisions (sprint 044): installation is a per-camera `intrinsics.json` via `LensStore` rather than a field inside `calibration.json`, so a field `calibrate` can never overwrite a measured lens with a stale in-memory copy (this removes the old restart-the-daemon caveat); the daemon, not a client, owns the camera and the vision; the GUI/CLI/MCP are thin pollers of the same four commands; the good-enough metric measures *worst undistortion disagreement across the field*, not reprojection RMS alone, because RMS looks fine while the corners are badly modelled.

## 5. Interfaces

### Exposes
`DaemonService.start_intrinsics / get_intrinsics_status / finish_intrinsics / abort_intrinsics`, via the four RPCs and `IntrinsicsStatus` message (`daemon/DESIGN.md`, `proto/DESIGN.md`).

### Consumes
`camera.Camera.read()` through an injected frame source; `calibration.lens.LensStore` and `LensModel` through an injected install callback; `config.interfaces.CalibrationTarget` (new edge `intrinsics -> config`, value type only, used by `target.py`).

## 6. Open Questions / Known Limitations

- The default board is one specific printed board; other boards need `--board` or the env var.
- Thresholds were tuned on this project's cameras (ov9782/ov9281-class).
- Reading the camera from a session shares the capture path with detection; heavy use at the same time is not load-tested.
