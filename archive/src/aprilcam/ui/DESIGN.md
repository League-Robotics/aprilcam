# UI (Display Helpers)

**Owner:** Eric Busboom · **Last reviewed:** 2026-07-29 · **Status:** deprecated

---

## 1. Purpose

OpenCV-based rendering helpers shared by anything that shows a playfield frame to a human: `display.PlayfieldDisplay` turns raw camera frames into a deskewed/cropped display image and draws every overlay (tag boxes, velocity arrows, world-coordinate crosses, status panel, agent paths, live overlay elements). The seam is *presentation*: it consumes detection results and calibration, and owns only how they look. `tui.py` (a Rich terminal dashboard) also lives here but is dead code — see §6.

## 2. Orientation

`PlayfieldDisplay` wraps a `core.PlayfieldBoundary` and a cv2 window. Per frame: `update()` refreshes the boundary polygon, lazily builds and caches the metric deskew transform (`M_deskew`, `deskew_size`, sourced from `playfield.deskew_transform()` so warp math is single-sourced in calibration/geometry), optionally undistorts (when a calibration with intrinsics is supplied and `undistort=True`), and returns the display image in one of three modes — `full`, `crop` (polygon bounding box + padding), or `deskew` (perspective warp). All draw methods route coordinates through `_map_points_to_display`, which applies the same mode-dependent transform so overlays land correctly in any view. Consumers: `core/aprilcam.py` (the legacy in-process pipeline) and `cli/view_cli.py` (the live viewer, which seeds the boundary/homography from the daemon's tag stream rather than local detection).

## 3. Constraints and Invariants

- **World-coordinate draw inputs are A1-centred cm; `origin_x`/`origin_y` are added back before applying `H_inv`** — the homography maps raw corner-origin world coords. Feeding A1-centred values straight into `H_inv` shifts every overlay by half the field.
- **The RGB→BGR conversion happens here, at the draw site, and nowhere earlier** — `draw_paths` and `draw_live_overlay` receive RGB (per the `server/paths.py` and overlay-element contracts) and swap channels for cv2. Converting upstream double-swaps.
- **The deskew transform is computed once and cached** (`M_deskew is not None` guard) — it does not track a moving camera; a recalibration requires a new `PlayfieldDisplay` (or clearing the cache). Cheap by design: the viewer runs this per frame at stream rate.
- **Overlay TTL is enforced at draw time** — `draw_live_overlay` is a no-op once `now - timestamp > ttl`, which is what makes robot overlays at 5–10 Hz self-cleaning.
- **All drawing is defensive** — every element/tag draw is wrapped in `try/except: pass`; a malformed path or overlay element must never kill the render loop.
- **Symbol/pixel sizes derive from world size** — `size_cm`/`radius_cm` are projected through the homography at the element's position (not a global px-per-cm), so symbols stay metrically sized under perspective.

## 4. Design

`_map_points_to_display` is the heart: `deskew` mode applies `cv.perspectiveTransform` with the cached matrix; `crop` subtracts the crop origin; `full` passes through. Draw methods therefore compute in *source-frame* pixels and map at the end. `draw_overlays` renders tag quads with a "roof" apex showing the tag's forward direction, velocity arrows scaled from `vel_px`, an optional gripper dot (robot tag projected `gripper_offset_cm` forward in world space), and a yellow cross at the height-corrected world position. `draw_status_panel` composites a semi-transparent right-aligned status box (playfield/deskew/homography state, tag ids, path count, FPS). `draw_paths` runs two passes (lines, then symbols) over precomputed display points so a failed waypoint drops out of both. `draw_live_overlay` implements the seven overlay element types (arc/arrow/point/polyline/text/rect/polygon) with world-cm params. `cv2` is imported at module level — importing `aprilcam.ui.display` requires OpenCV, which is why the viewer currently needs the `[daemon]` extra.

## 5. Interfaces

### Exposes

- `PlayfieldDisplay(playfield, window_name, headless, deskew_overlay, robot_tag_id, gripper_offset_cm, calibration, undistort)` — `update(frame) -> display`, `prepare_display(frame)`, `show(display)`, `pause(frame)`, `draw_overlays(frame, tags, homography, origin_x, origin_y)`, `draw_status_panel(frame, tags, homography, num_paths, fps)`, `draw_paths(frame, paths_dict, playfield, homography, origin_x, origin_y)`, `draw_live_overlay(frame, overlay_frame, homography, origin_x, origin_y)`, `_update_deskew` / `_map_points_to_display` (used directly by `view_cli`). Origins are field half-dimensions (or the daemon-published `origin_x/origin_y` — consumers must use the stream's values, not recompute).
- `TagTableTUI` (via `ui/__init__.py`) — Rich table dashboard; no live callers.

### Consumes

- `core.PlayfieldBoundary` (polygon + `deskew_transform`) and `core.models.AprilTag` — owned by core.
- `CameraCalibration.undistort` (duck-typed via the `calibration` argument) — owned by calibration.
- `cv2`, `numpy`, `rich` (tui only) — third-party.

## 6. Open Questions / Known Limitations

- **`tui.py` is dead code**: `TagTableTUI` is re-exported by `ui/__init__.py` but imported by nothing else in the tree (`core/aprilcam.py`'s Rich TUI is a separate, private implementation). **v2 removes it** (spec §4.8, §6).
- **v2 disposition — this subsystem dissolves** (hence Status: deprecated). The drawing helpers (`draw_paths`, `draw_live_overlay`, world→pixel mapping, symbol rendering) fold into the **client drawing library** (spec §4.6), re-expressed cv2-free over daemon-published deskewed frames and stream metadata. The viewer-facing pieces (window management, deskew warp, status panel) go to the new **`view/` subsystem** (spec §4.8); with the daemon publishing the deskewed stream (spec §4.1), the local warp/undistort path — and this module's module-level cv2 import — disappear.
- The module-level `import cv2` makes every importer cv2-dependent, which currently contradicts the CLI's classification of `view` as an OpenCV-free client (documented in cli/DESIGN.md §6).
- The status panel hard-codes ArUco corner IDs 0–3 and treats ids ≥5 as AprilTags; interior/perimeter marker conventions (IDs 1–8 perimeter, ≥9 interior) have outgrown these assumptions — do not extend them, port them.
