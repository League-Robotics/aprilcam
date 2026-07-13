---
status: in-progress
sprint: '006'
tickets:
- 006-001
- 006-002
- 006-003
- 006-004
- 006-005
---

# Plan: Add Text, Rect, and Polygon Overlay Element Types

## Context

The live overlay system (Sprint 005) supports `arc`, `arrow`, `point`, and `polyline`.
Robot programs need to label tags, show distances and state names, mark bounding boxes,
and shade zones. Adding `text`, `rect`, and `polygon` closes these gaps.

`text` requires a new string field in the proto (`OverlayElement.text`). `rect` and
`polygon` need no proto changes — they fit in existing `params`. Proto regeneration is
required after the field addition.

---

## New Element Types

| type | params | new fields | description |
|------|--------|------------|-------------|
| `"text"` | `[x, y]` world cm anchor | `text` (string) | Label drawn at world position; `params[2]` optional font_scale (default 0.6) |
| `"rect"` | `[x1, y1, x2, y2]` world cm corners | — | Rectangle; `thickness=-1` fills |
| `"polygon"` | `[x0,y0, x1,y1, …]` world cm | — | Closed polyline; `thickness=-1` fills |

`text` reuses the existing `PlayfieldDisplay._draw_text_with_outline()` static method
(lines 130–133 of `display.py`) — exact same two-layer outline approach used for tag
ID labels.

---

## Files to Modify

### 1. `proto/aprilcam.proto`
Add `string text = 5;` to `OverlayElement`:
```protobuf
message OverlayElement {
  string         type      = 1;
  repeated float params    = 2;
  repeated int32 color     = 3;
  int32          thickness = 4;
  string         text      = 5;  // content for "text" type elements
}
```

### 2. Regenerate bindings
```bash
uv run python -m grpc_tools.protoc \
  -I proto --python_out=src/aprilcam/proto \
  --grpc_python_out=src/aprilcam/proto proto/aprilcam.proto
```
Then fix `aprilcam_pb2_grpc.py` import: `from aprilcam.proto import aprilcam_pb2 as aprilcam__pb2`.

### 3. `src/aprilcam/ui/display.py` — `draw_live_overlay()` (lines 524–588)
Add three new `elif` branches inside the per-element loop, after the `"polyline"` case:

**text** (reuses `_draw_text_with_outline`):
```python
elif elem.type == "text":
    x, y = p[0], p[1]
    font_scale = float(p[2]) if len(p) > 2 else 0.6
    cx_d, cy_d = _w2d(x, y)
    thickness = max(1, t)
    self._draw_text_with_outline(
        frame, elem.text, (cx_d, cy_d),
        color=bgr, font_scale=font_scale, thickness=thickness,
    )
```

**rect**:
```python
elif elem.type == "rect":
    x1, y1, x2, y2 = p[0], p[1], p[2], p[3]
    pt1 = _w2d(x1, y1)
    pt2 = _w2d(x2, y2)
    fill = cv.FILLED if t < 0 else t
    cv.rectangle(frame, pt1, pt2, bgr, fill)
```

**polygon** (closed polyline, supports fill):
```python
elif elem.type == "polygon":
    pts_world = [(p[i], p[i + 1]) for i in range(0, len(p) - 1, 2)]
    disp_pts = np.array([_w2d(x, y) for x, y in pts_world], dtype=np.int32)
    if t < 0:
        cv.fillPoly(frame, [disp_pts], bgr)
    else:
        cv.polylines(frame, [disp_pts], isClosed=True, color=bgr, thickness=t)
```

### 4. `src/aprilcam/client/control.py` — `publish_overlay()` (lines 298–305)
Read the `text` field from the element dict:
```python
aprilcam_pb2.OverlayElement(
    type=e["type"],
    params=list(e.get("params", [])),
    color=list(e.get("color", [255, 255, 255])),
    thickness=int(e.get("thickness", 2)),
    text=str(e.get("text", "")),        # ← add this line
)
```

### 5. `src/aprilcam/server/mcp_server.py` — `set_live_overlay` docstring (line ~3708)
Add `text` element documentation and update element types list:
```
type (str): "arc", "arrow", "point", "polyline", "text", "rect", or "polygon"
```
For `"text"`:
```
text:     params=[x, y] optionally [x, y, font_scale]; requires text field (str)
rect:     params=[x1, y1, x2, y2]; thickness=-1 fills
polygon:  params=[x0,y0, x1,y1, ...]; closed shape; thickness=-1 fills
```

### 6. `src/aprilcam/ROBOT_API_GUIDE.md` — element types table
Add three rows:
| `"text"` | `[x, y]` or `[x, y, font_scale]` | Text label at world position; set `"text"` key in element dict |
| `"rect"` | `[x1, y1, x2, y2]` | Rectangle; `-1` fills |
| `"polygon"` | `[x0, y0, x1, y1, …]` | Closed polygon; `-1` fills |
Update the example dict to show a `text` element.

### 7. `tests/test_display_overlay.py`
Add four tests:
- `test_text_draws` — element with `type="text"`, `text="hello"`, verify frame non-zero
- `test_text_empty_string` — empty string, should not raise
- `test_rect_draws` — fills a region, verify non-zero
- `test_polygon_draws` — closed filled polygon, verify non-zero

---

## Implementation Order

1. `proto/aprilcam.proto` — add `text` field
2. Regenerate bindings, fix import
3. `display.py` — add three draw branches
4. `control.py` — add `text` field passthrough
5. `mcp_server.py` — update docstring
6. `ROBOT_API_GUIDE.md` — update table
7. `tests/test_display_overlay.py` — add tests
8. Run `uv run pytest tests/ --ignore=tests/system -q`
9. Commit, bump version

---

## Verification

```bash
# Import smoke
uv run python -c "from aprilcam.proto import aprilcam_pb2; print(aprilcam_pb2.OverlayElement(text='hi'))"
uv run python -c "from aprilcam.ui.display import PlayfieldDisplay; print('ok')"

# Tests
uv run pytest tests/test_display_overlay.py -v
uv run pytest tests/ --ignore=tests/system -q

# Manual (with live camera + view open)
uv run python -c "
from aprilcam.config import Config
from aprilcam.client.control import DaemonControl
dc = DaemonControl.connect_default(Config.load())
cam = dc.list_cameras()[0]
dc.publish_overlay(cam, [
    {'type': 'text',    'params': [60, 45], 'text': 'Hello World', 'color': [255, 220, 0]},
    {'type': 'rect',    'params': [40, 30, 80, 60], 'color': [0, 200, 255], 'thickness': 2},
    {'type': 'polygon', 'params': [60,20, 80,50, 40,50], 'color': [255,0,100], 'thickness': -1},
], ttl=5.0)
dc.close()
"
```
