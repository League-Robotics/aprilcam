---
status: in-progress
sprint: 008
tickets:
- 008-001
- 008-002
---

# Parallax correction and calibration restructuring

## Context

Tags mounted on robots are ~118 mm above the playfield. The camera's homography
was calibrated assuming all tags lie flat (z=0), so elevated tags appear shifted
in world coordinates — the camera sees them displaced toward the camera's
nadir point. This needs a parallax correction: project the observed tag position
back to the playfield along the viewing ray, accounting for the tag's actual height.

Two supporting changes are required first:
1. Restructure `calibration.json` to store playfield dimensions in a `playfield`
   sub-dict and add a `camera_position` sub-dict.
2. Store per-tag height defaults in `calibration.json` under `tag_heights`.

## Parallax correction math

Camera is at world position `(cx, cy, H)` (offsets from field center, height in cm).
Uncorrected world position from homography: `(wx0, wy0)` (on z=0 plane).
Tag is at height `h`. Corrected position on playfield:

```
x = wx0 + (h/H) * (cx - wx0)
y = wy0 + (h/H) * (cy - wy0)
```

When `h=0` the formula is identity (no correction). When `h=H` the tag appears
to be at the camera's nadir. This is the standard ray-plane intersection.

## Changes

### 1. `src/aprilcam/calibration/calibration.py`

**Add `CameraPosition` dataclass**:
```python
@dataclass
class CameraPosition:
    x_offset: float = 0.0   # cm from field center, positive = right
    y_offset: float = 0.0   # cm from field center, positive = up
    height: float = 0.0     # cm above playfield
```

**Update `CameraCalibration`**:
- Add `camera_position: Optional[CameraPosition] = None`
- Add `tag_heights: dict[int, float] = field(default_factory=dict)`  
  (maps tag_id → height above playfield in cm)
- Replace `field_width_cm: float` / `field_height_cm: float` with
  `playfield_width_cm: float` / `playfield_height_cm: float` (keeping the
  same float values, just renamed on the object)
- Add method:
  ```python
  def correct_world_for_height(
      self, wx: float, wy: float, tag_height_cm: float
  ) -> tuple[float, float]:
      if self.camera_position is None or self.camera_position.height == 0.0:
          return wx, wy
      H = self.camera_position.height
      cx = self.camera_position.x_offset
      cy = self.camera_position.y_offset
      r = tag_height_cm / H
      return (wx + r * (cx - wx), wy + r * (cy - wy))
  ```

**Update `load_calibration_from_camera_dir()`**:
- Read `playfield.width` / `playfield.height`; fall back to `field_width_cm` /
  `field_height_cm` for old files (backward compat on read)
- Read `camera_position` dict → `CameraPosition`
- Read `tag_heights` dict (keys are string tag IDs → convert to `int`)

**Update `save_calibration_to_camera_dir()`**:
- Write `playfield: {width: ..., height: ...}` (new format only)
- Write `camera_position: {x_offset: ..., y_offset: ..., height: ...}`
- Write `tag_heights: {id: height_cm, ...}`
- Remove old `field_width_cm` / `field_height_cm` top-level keys

**Update `load_field_dimensions_from_camera_dir()`**:
- Read `playfield.width` / `playfield.height`; fall back to old keys

### 2. Grep and update all `field_width_cm` / `field_height_cm` usages

All code that reads `.field_width_cm` or `.field_height_cm` from a
`CameraCalibration` object must switch to `.playfield_width_cm` /
`.playfield_height_cm`. Known locations:
- `src/aprilcam/daemon/camera_pipeline.py`
- `src/aprilcam/server/mcp_server.py` (create_playfield, calibrate_playfield)
- `src/aprilcam/ui/display.py`
- Any tests

### 3. `src/aprilcam/daemon/camera_pipeline.py` — apply correction per tag

After computing `world_xy` for each tag (wherever `world_xy` is set from the
homography), apply height correction using the pipeline's calibration:

```python
if self._calibration and self._calibration.camera_position:
    tag_h = self._calibration.tag_heights.get(tr.id, 0.0)
    if tag_h > 0.0 and tr.world_xy is not None:
        tr.world_xy = self._calibration.correct_world_for_height(
            tr.world_xy[0], tr.world_xy[1], tag_h
        )
```

### 4. `src/aprilcam/server/mcp_server.py` — `calibrate_playfield` new params

Add parameters to the `calibrate_playfield` MCP tool:
- `camera_height_cm: float` — required (no default; must be measured and set)
- `camera_x_offset_cm: float = 0.0`
- `camera_y_offset_cm: float = 0.0`

These are stored in `calibration.json` under `camera_position` when calibration
is saved.

### 5. `src/aprilcam/server/mcp_server.py` — `get_tags` height override

Add optional `tag_heights_json: str | None = None` parameter to `get_tags`.
When provided, it is a JSON dict mapping tag_id (str) → height_cm (float).
Overrides persistent `calibration.tag_heights` for that call only.

Apply correction to each returned tag's `world_xy`:
```python
# Merge persistent + per-request heights
heights = dict(calibration.tag_heights)  # persistent
if tag_heights_json:
    heights.update({int(k): v for k, v in json.loads(tag_heights_json).items()})
# Apply to each tag
for tag in tags:
    h = heights.get(tag["id"], 0.0)
    if h > 0.0 and tag.get("world_xy"):
        tag["world_xy"] = list(
            calibration.correct_world_for_height(*tag["world_xy"], h)
        )
```

### 6. Streaming: `stream_tags` / `start_detection`

The daemon pipeline applies the persistent `tag_heights` correction automatically
(change #3). No new parameters needed for streaming — the correction is baked in
via the calibration. To change tag heights at runtime, update `calibration.json`
and call the existing `reload_calibration` tool.

Document this in the tool docstrings.

## Critical files

- `src/aprilcam/calibration/calibration.py` — struct + math + load/save
- `src/aprilcam/daemon/camera_pipeline.py` — apply correction per tag
- `src/aprilcam/server/mcp_server.py` — calibrate_playfield params, get_tags override
- `data/aprilcam/cameras/*/calibration.json` — format changes (old files still load)
- Any file reading `.field_width_cm` / `.field_height_cm` on a `CameraCalibration`

## Verification

1. Update `calibration.json` manually to add `camera_position.height = 118.0` and
   `tag_heights = {"5": 11.8}` (robot tag ID 5 is 11.8 cm high).
2. Restart daemon; open camera, create playfield.
3. Move robot to a known position near an edge; call `get_tags` — verify world_xy
   is closer to actual position than before.
4. Call `get_tags(source_id=..., tag_heights_json='{"5": 0}')` — verify result
   matches the uncorrected (flat) position.
5. Run `uv run pytest tests/ -q` — all tests pass.
