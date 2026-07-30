# Vision

**Owner:** Eric Busboom · **Last reviewed:** 2026-07-29 · **Status:** in-flux

---

## 1. Purpose

Stateless, frame-level computer vision for the daemon pipeline: non-tag object
detection (squares/cubes), color classification, and the OpenCV version shim
that lets the same detection code run on both distro OpenCV 4.6 and pip 4.7+.
The seam is *pure pixel work vs. stateful orchestration*: everything here
takes a frame (plus optional homography/polygon context) and returns records —
no ring buffers, no camera ownership, no persistence. It alone owns the
question "what non-tag objects are in this frame, and what color are they."

## 2. Orientation

Three modules today:

- **`objects.py`** — the detection dataclasses and the square detector.
  `ObjectRecord` (frozen dataclass: pixel center, bbox, area, optional
  world_xy, color, type, confidence) is the lingua franca for every object
  producer/consumer. `FrameResult` wraps a frame's tag list plus `.objects`,
  `.timestamp`, `.frame_index`. `SquareDetector` finds square-ish bright
  contours on a dark field. `ObjectFuser` merges B&W-camera geometry with
  color-camera color labels. `ColorCameraThread` is a daemon-only ~5 fps
  second-camera sampling thread.
- **`color_classifier.py`** — `LABColorClassifier` (per-color Gaussian models
  in CIELAB, Mahalanobis winner-takes-all) and the legacy HSV-threshold
  `ColorClassifier`.
- **`aruco_compat.py`** — `make_aruco_detector`, the 4.6↔4.7+ ArUco API shim.
  Every ArUco/AprilTag detector construction in the codebase goes through it.

Flow: the daemon pipeline hands a grayscale frame (plus current tag corners,
playfield polygon, homography) to `SquareDetector.detect`; optionally a color
camera thread classifies frames and feeds `ObjectFuser`, which stamps color
names onto the B&W detections by world-coordinate proximity.

## 3. Constraints and Invariants

- **`FrameResult` subclasses `list`** so `isinstance(result, list)` holds and
  existing tag-list consumers work unmodified while new consumers read
  `.objects` / `.timestamp`. Changing it to a plain dataclass silently breaks
  every caller that iterates the result as a tag list.
- **`aruco_compat` exists because the Pi must stay on distro OpenCV 4.6** —
  the Ubuntu `python3-opencv` build has GStreamer support (needed for the
  libcamera capture backend) that pip wheels lack. Never "clean up" call
  sites to use `cv2.aruco.ArucoDetector` directly; that breaks the Pi.
  cv2 is imported lazily inside the shim so cv2-free modules can import it.
- **`ColorCameraThread` is daemon-only** — it opens `cv2.VideoCapture`
  directly and therefore may only run inside the process that owns the
  hardware (the single-camera-owner rule). It must never be reachable from
  MCP or CLI paths.
- **`ObjectRecord` is frozen** — fusion uses `dataclasses.replace`, never
  mutation. Records may be shared across threads (fuser map ↔ pipeline).
- **Illumination flattening is division, not subtraction**
  (objects.py:108-115): illumination is multiplicative, so dividing by the
  Gaussian-blurred low-frequency field recovers reflectance — a cube in a
  glare zone and one in shadow threshold identically. A subtractive high-pass
  reintroduces the lighting dependence.
- **`LABColorClassifier` exists for the colors HSV cannot separate** —
  near-white yellow, dark black, low-saturation magenta. Winner-takes-all
  over all models (including the optional `__background__` model) is what
  prevents two colors claiming the same region; per-color independent
  thresholding regresses that.
- **Stateless-per-frame is the contract** — the only caches are advisory
  (`SquareDetector._known_tag_corners` for flicker-proof tag exclusion,
  `ObjectFuser._color_map` with staleness expiry). Nothing here may hold
  camera handles (ColorCameraThread excepted, see above) or frame history.

## 4. Design

**SquareDetector.** Pipeline per frame: Gaussian-blur illumination estimate
(kernel `highpass_ksize`, default 51) → divide out → binary threshold
(default 155) → 3×3 morphological open → external contours → filter by area
window (200–800 px²), aspect (<2.0), solidity (>0.7) → reject centers outside
the playfield polygon shrunk inward by `border_margin` (excludes edge
markers) → reject centers inside tag polygons expanded by `tag_margin`
(default 1.8×, using both current-frame corners and the persistent cache via
`update_known_tags`, so a tag that flickers out for a frame still masks its
region) → optional exclusion point/radius → world_xy via homography when
provided. Filters are ordered cheapest-first.

**Color fusion.** The B&W tag camera has the good geometry; the color camera
has the color. `ObjectFuser` keys a map by world coordinates quantized to
0.1 cm and matches within `match_radius` (5.0 cm, generous because the two
cameras' calibrations disagree by a few cm). `clear_stale` expires entries
after 5 s so a moved cube doesn't inherit a ghost color. `ColorCameraThread`
runs classification at ~5 fps — color changes slowly; there is no reason to
burn CPU at frame rate.

**LABColorClassifier.** Calibration samples ROIs at operator-supplied pixel
positions across a burst of frames, building per-color (mean, inverse
covariance) in LAB with +0.5·I regularization. Classification computes the
Mahalanobis distance from every pixel to every model, assigns each pixel to
its nearest model, and thresholds that minimum (default 4.5); background
pixels win the `__background__` model and are suppressed. Contours of each
color mask become `ObjectRecord`s. `classify_at_point` answers "what color is
here" from an ROI mean with a 3× relaxed threshold. The HSV `ColorClassifier`
(8 named colors, red modeled as two hue ranges across the 180° wrap) is the
zero-calibration fallback.

## 5. Interfaces

### Exposes

- `ObjectRecord(center_px, bbox, area_px, world_xy=None, color="unknown", object_type="cube", confidence=1.0)` — frozen.
- `FrameResult(tags, objects=None, timestamp=0.0, frame_index=0)` — is-a list
  of tags; carries `.objects`, `.timestamp`, `.frame_index`.
- `SquareDetector(min_area, max_area, threshold, tag_margin, border_margin, highpass_ksize)`
  with `detect(gray, homography=None, tag_corners=None, exclusion_point=None, exclusion_radius=50, playfield_polygon=None) -> list[ObjectRecord]`
  and `update_known_tags(tag_records)` (call every frame).
- `ObjectFuser(match_radius=5.0)` — `update_colors(color_objects)`,
  `fuse(bw_objects) -> list[ObjectRecord]`, `clear_stale(max_age_seconds=5.0)`.
- `ColorCameraThread(camera_index, fuser, classifier, homography=None, fps=5.0)`
  — `start()` / `stop()`. Daemon-only.
- `LABColorClassifier(min_area, max_area, mahal_threshold)` —
  `calibrate(frames_bgr, color_positions, roi_radius=12, background_positions=None, ...)`,
  `classify(frame_bgr, homography=None) -> list[ObjectRecord]`,
  `classify_at_point(frame_bgr, x, y, radius=15) -> str`.
- `ColorClassifier(color_ranges=None, min_area, max_area)` — same `classify` /
  `classify_at_point` shape; `DEFAULT_COLOR_RANGES` table.
- `make_aruco_detector(dictionary, params)` — uniform
  `detectMarkers(image) -> (corners, ids, rejected)` on OpenCV 4.6 and 4.7+.

### Consumes

- OpenCV (cv2, hard import in objects/color_classifier; lazy in
  aruco_compat), NumPy. Tag records passed in must expose `.id` and
  `.corners_px` (see `aprilcam.core.detector`).

## 6. Open Questions / Known Limitations

- **v2 consolidation:** the stateless detector/tracker/boundary code
  currently in `core/` (detector, LK tracker, playfield boundary) moves into
  this package, and the stateful per-camera pipeline moves into `daemon/`;
  the duplicate second pipeline (`core/pipeline.py`, `core/motion.py`) is
  removed (spec §4.3, §6). This package becomes the single home for
  frame-level vision.
- **Fusion is nearest-neighbor greedy** — two cubes closer together than the
  calibration disagreement (~5 cm) can swap colors. Acceptable for current
  playfields; revisit if object density grows.
- **`ColorCameraThread` swallows all per-frame exceptions** (objects.py:304)
  — deliberate (a flaky second camera must not kill fusion) but it hides
  persistent failures; no health signal is surfaced.
- **`SquareDetector._known_tag_corners` never expires** — a tag removed from
  the field keeps masking its last region until recalibration/restart.
  Harmless for static tag layouts; wrong if tags are frequently removed.
- **LAB calibration is session-local** — models are not persisted; every
  daemon restart requires re-calibration before color classification works.
