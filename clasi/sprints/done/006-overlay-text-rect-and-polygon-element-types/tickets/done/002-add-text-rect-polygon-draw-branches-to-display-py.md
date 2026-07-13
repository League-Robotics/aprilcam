---
id: '002'
title: Add text, rect, polygon draw branches to display.py
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
depends-on:
- '001'
github-issue: ''
issue: plan-add-text-rect-and-polygon-overlay-element-types.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Add text, rect, polygon draw branches to display.py

## Description

Add three new `elif` branches inside the per-element loop in
`PlayfieldDisplay.draw_live_overlay()`, after the existing `"polyline"` case.
`"text"` reuses the existing `_draw_text_with_outline()` static method.
`"rect"` and `"polygon"` use standard OpenCV drawing calls.

Depends on ticket 001 — the proto bindings must be regenerated first so that
`elem.text` is a valid attribute.

## Acceptance Criteria

- [x] `"text"` branch renders a text label at the world-cm anchor using `_draw_text_with_outline()`
- [x] Optional `params[2]` overrides the default `font_scale` of 0.6
- [x] `"rect"` branch draws a rectangle; `thickness=-1` fills
- [x] `"polygon"` branch draws a closed polygon; `thickness=-1` fills
- [x] Unknown/missing `elem.text` does not raise (falls through try/except)
- [x] `uv run pytest tests/ --ignore=tests/system -q` passes

## Implementation Plan

### Approach

Add three `elif` branches inside the existing try/except per-element loop in
`draw_live_overlay()` (around line 582 of `src/aprilcam/ui/display.py`), after
the `"polyline"` case and before the closing `except Exception: pass`.

### Files to Modify

| File | Change |
|------|--------|
| `src/aprilcam/ui/display.py` | Add three `elif` branches in `draw_live_overlay()` |

### Draw Code to Add

After the `elif elem.type == "polyline":` block (around line 585), add:

```python
elif elem.type == "text":
    x, y = p[0], p[1]
    font_scale = float(p[2]) if len(p) > 2 else 0.6
    cx_d, cy_d = _w2d(x, y)
    self._draw_text_with_outline(
        frame, elem.text, (cx_d, cy_d),
        color=bgr, font_scale=font_scale, thickness=max(1, t),
    )

elif elem.type == "rect":
    x1, y1, x2, y2 = p[0], p[1], p[2], p[3]
    pt1 = _w2d(x1, y1)
    pt2 = _w2d(x2, y2)
    cv.rectangle(frame, pt1, pt2, bgr, cv.FILLED if t < 0 else t)

elif elem.type == "polygon":
    pts_world = [(p[i], p[i + 1]) for i in range(0, len(p) - 1, 2)]
    disp_pts = np.array([_w2d(x, y) for x, y in pts_world], dtype=np.int32)
    if t < 0:
        cv.fillPoly(frame, [disp_pts], bgr)
    else:
        cv.polylines(frame, [disp_pts], isClosed=True, color=bgr, thickness=t)
```

### Testing Plan

- Regression: `uv run pytest tests/ --ignore=tests/system -q`
- New tests are added in ticket 005.

### Documentation Updates

None required for this ticket.
