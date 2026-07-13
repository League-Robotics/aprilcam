---
id: '004'
title: draw_paths rendering in PlayfieldDisplay
status: done
use-cases:
- SUC-005
depends-on:
- '003'
github-issue: ''
issue: agent-drawn-paths-on-the-aprilcam-live-view.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# T004: draw_paths rendering in PlayfieldDisplay

## Description

Implement `PlayfieldDisplay.draw_paths(frame, paths, playfield, homography)`
in `src/aprilcam/ui/display.py`. This is a new method added after
`draw_overlays` (around line 262). It handles all 8 symbol types, lines-first
draw order, per-waypoint `size_cm` pixel scaling via inverse homography, and
RGB→BGR conversion at the OpenCV call boundary.

T003 already added the call site in `_child_main`; this ticket provides the
full implementation to replace any stub left there.

## Acceptance Criteria

- [x] `draw_paths(self, frame, paths, playfield, homography)` exists on
      `PlayfieldDisplay` and is callable.
- [x] If `homography` is `None`, the method returns immediately (no-op, no
      crash). This handles uncalibrated playfields.
- [x] If `paths` is empty, the method returns immediately (no-op).
- [x] **World → source pixel**: for each waypoint, compute source pixel by
      applying `H_inv = np.linalg.inv(homography)` to the homogeneous world
      coordinate `[x, y, 1]` and dividing by the third component. Pattern
      from `src/aprilcam/core/playfield.py` lines 451-457.
- [x] **Source pixel → display pixel**: pass through
      `self._map_points_to_display(np.array([[sx, sy]], dtype=np.float32))`
      to get display-space coordinates (handles deskew/crop modes).
- [x] **Pixel radius for `size_cm`**: map both `(x, y)` and
      `(x + size_cm/2, y)` through the same pipeline; the pixel radius `r` is
      the Euclidean distance between the two resulting display points, rounded
      to an int (minimum 1).
- [x] **Draw order**: for each path, draw all line segments first (so symbols
      cover line endpoints), then draw all waypoint symbols.
- [x] **Lines**: `cv.line(frame, pt_from, pt_to, bgr_line_color, thickness=2, lineType=cv.LINE_AA)`.
      Last waypoint's `line_color` is unused (no line after last waypoint).
- [x] **All 8 symbol types** render correctly:

  | Symbol | Implementation |
  |--------|----------------|
  | `circle` | `cv.circle(frame, center, r, color, thickness=2, lineType=cv.LINE_AA)` |
  | `filled_circle` | `cv.circle(frame, center, r, color, thickness=cv.FILLED, lineType=cv.LINE_AA)` |
  | `square` | `cv.rectangle(frame, (cx-r, cy-r), (cx+r, cy+r), color, thickness=2, lineType=cv.LINE_AA)` |
  | `filled_square` | `cv.rectangle(frame, (cx-r, cy-r), (cx+r, cy+r), color, thickness=cv.FILLED, lineType=cv.LINE_AA)` |
  | `triangle` | `cv.polylines(frame, [pts_triangle], True, color, 2, cv.LINE_AA)` where pts are apex (top), lower-left, lower-right |
  | `filled_triangle` | `cv.fillPoly(frame, [pts_triangle], color)` |
  | `x` | two `cv.line` calls: `(cx-r, cy-r)→(cx+r, cy+r)` and `(cx+r, cy-r)→(cx-r, cy+r)`, thickness=2 |
  | `none` | no symbol drawn; function returns after lines pass through this vertex |

- [x] **RGB→BGR**: colors from the path dict are RGB triples `[R, G, B]`;
      convert to `(B, G, R)` tuple immediately before each `cv.*` call.
      No conversion elsewhere.
- [x] Exceptions per waypoint are caught and skipped (defensive — off-screen
      coordinates or degenerate homography must not crash the render loop).
- [x] `paths` parameter accepts `dict[str, dict]` (plain dicts, not `Path`
      dataclasses) — the child consumes what came over the pipe.

## Implementation Plan

### Approach

The method signature accepts `paths: dict` (values are path dicts as produced
by `Path.to_dict()`). Iterate `paths.values()`. For each path dict, iterate
`path["waypoints"]`.

Inverse-homography pattern (from `playfield.py` lines 451-457):
```python
H_inv = np.linalg.inv(homography)
hvec = H_inv @ np.array([x, y, 1.0])
sx, sy = hvec[0] / hvec[2], hvec[1] / hvec[2]
src_pt = np.array([[sx, sy]], dtype=np.float32)
disp_pt = self._map_points_to_display(src_pt).reshape(2)
cx, cy = int(round(disp_pt[0])), int(round(disp_pt[1]))
```

Pixel-radius scaling:
```python
hvec2 = H_inv @ np.array([x + size_cm / 2, y, 1.0])
sx2, sy2 = hvec2[0] / hvec2[2], hvec2[1] / hvec2[2]
src_pt2 = np.array([[sx2, sy2]], dtype=np.float32)
disp_pt2 = self._map_points_to_display(src_pt2).reshape(2)
r = max(1, int(round(np.linalg.norm(disp_pt2 - disp_pt))))
```

Two-pass rendering (lines first, then symbols):
```python
# Pass 1: lines
for i, wp in enumerate(waypoints[:-1]):
    # compute disp_pt for wp and waypoints[i+1]
    line_color_bgr = (wp["line_color"][2], wp["line_color"][1], wp["line_color"][0])
    cv.line(frame, pt_i, pt_i_next, line_color_bgr, 2, cv.LINE_AA)

# Pass 2: symbols
for wp in waypoints:
    # compute cx, cy, r
    if wp["symbol"] == "none":
        continue
    sym_color_bgr = (wp["symbol_color"][2], wp["symbol_color"][1], wp["symbol_color"][0])
    # dispatch on symbol name
```

Cache computed display points after pass 1 to avoid recomputing in pass 2.

Triangle points (equilateral-ish, apex up):
```python
pts_tri = np.array([
    [cx, cy - r],           # apex
    [cx - r, cy + r],       # lower-left
    [cx + r, cy + r],       # lower-right
], dtype=np.int32)
```

### Files to Modify

**`src/aprilcam/ui/display.py`** — add `draw_paths` method after
`draw_overlays` (~line 262). No existing code changed.

### Files to Create

None.

### Testing Plan

`draw_paths` requires an actual numpy frame array and homography matrix —
unit-testable without a camera.

Write `tests/test_draw_paths.py`:
- `test_draw_paths_noop_no_homography` — call with `homography=None`;
  frame must be unchanged.
- `test_draw_paths_noop_empty_paths` — call with `paths={}`;
  frame must be unchanged.
- `test_draw_paths_all_symbols_no_crash` — construct a simple identity-like
  homography, a 500×500 black frame, and one path with 8 waypoints (one of
  each symbol); call `draw_paths`. Assert no exception and frame is not
  all-black (pixels changed).
- `test_draw_paths_none_symbol_skips_marker` — a path with one `"none"`
  waypoint; verify no cv primitive error (lines still drawn if two waypoints).
- `test_draw_paths_color_is_bgr` — use `symbol_color=[255,0,0]` (red in
  RGB); verify the drawn pixel at the center is blue-dominant in BGR
  (OpenCV flipped), confirming RGB→BGR conversion.

- **Existing tests to run**: `uv run pytest` (full suite).
- **Verification command**: `uv run pytest tests/test_draw_paths.py -v`

### Documentation Updates

Add a docstring to `draw_paths` describing the coordinate pipeline (world cm
→ source pixel via H_inv → display pixel via `_map_points_to_display`) and
the RGB→BGR convention. Note the no-op behavior when `homography` is None.
