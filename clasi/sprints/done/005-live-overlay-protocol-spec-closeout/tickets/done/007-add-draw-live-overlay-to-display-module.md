---
id: '007'
title: Add draw_live_overlay to display module
status: done
use-cases:
  - SUC-002
depends-on:
  - '003'
github-issue: ''
issue: plan-live-overlay-via-tag-stream-socket.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Add draw_live_overlay to display module

## Description

Add `draw_live_overlay(frame, overlay_frame, homography)` and a private helper
`_world_to_disp_with_hinv(x, y, H_inv)` to `src/aprilcam/ui/display.py`.

The method renders arc, arrow, point, and polyline overlay elements onto a frame.
All coordinates are world cm; the method maps them to display pixels via the
inverse homography. It handles TTL expiry, non-square homography for arc ellipses,
and silently skips unknown element types.

This ticket can proceed in parallel with ticket 006 since it only depends on the
generated proto bindings (ticket 003), not on the gRPC wiring.

## Acceptance Criteria

- [x] `draw_live_overlay(frame, overlay_frame, homography)` added to the display
      module.
- [x] Returns immediately (no-op) when `homography is None`.
- [x] Returns immediately (no-op) when `time.time() - overlay_frame.timestamp > overlay_frame.ttl`.
- [x] Private `_world_to_disp_with_hinv(x, y, H_inv)` helper maps world cm to
      integer display pixel coordinates.
- [x] `arc` elements: renders `cv2.ellipse()` using H_inv-mapped center and
      radii computed from unit vectors along both world axes.
- [x] `arrow` elements: renders `cv2.arrowedLine(tipLength=0.2)` from mapped
      tail to mapped head.
- [x] `point` elements: renders `cv2.circle(FILLED)` with world-cm radius mapped
      to display pixels.
- [x] `polyline` elements: renders `cv2.polylines(isClosed=False)` from mapped
      point list.
- [x] Unknown element types are silently skipped (no exception).
- [x] Each element rendering is wrapped in try/except (matches `draw_paths` style).
- [x] Unit tests cover: TTL expiry returns without drawing; arc, arrow, point,
      polyline each called with minimal valid params; unknown type skipped.
- [x] `uv run pytest tests/` passes.

## Implementation Plan

### Approach

1. Read `src/aprilcam/ui/display.py` to understand existing structure, how
   `draw_paths()` uses the homography, and where to place the new method.
2. Extract (or reuse) the world-to-display coordinate mapping logic as
   `_world_to_disp_with_hinv(x, y, H_inv)`. If `draw_paths` already has this
   inline, refactor it to use the helper.
3. Implement `draw_live_overlay()` with TTL check first, then dispatch per element.

**Arc pipeline**:
```python
H_inv = np.linalg.inv(homography)
cx_d, cy_d = _world_to_disp_with_hinv(cx, cy, H_inv)
rx_d, ry_d = _world_to_disp_with_hinv(cx + r, cy, H_inv)
ry_dx, ry_dy = _world_to_disp_with_hinv(cx, cy + r, H_inv)
rx = int(np.linalg.norm([rx_d - cx_d, ry_d - cy_d]))
ry = int(np.linalg.norm([ry_dx - cx_d, ry_dy - cy_d]))
angle = np.degrees(np.arctan2(ry_d - cy_d, rx_d - cx_d))
cv2.ellipse(frame, (cx_d, cy_d), (rx, ry), angle, start_deg, end_deg, color, thickness)
```

4. Write unit tests in `tests/test_display_overlay.py` (or add to existing
   display tests if present). Use a blank numpy frame and a simple identity-like
   homography.

### Files to Modify

- `src/aprilcam/ui/display.py`

### Files to Create

- `tests/test_display_overlay.py` (if no existing display test file)

### Testing Plan

- Unit tests: TTL expiry, each element type, unknown type skip.
- `uv run pytest tests/`

### Documentation Updates

None.
