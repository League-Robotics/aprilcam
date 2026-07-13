---
id: "001"
title: "Calibration struct changes and JSON format"
status: done
use-cases:
  - SUC-001
  - SUC-002
depends-on: []
github-issue: ""
issue: "parallax-correction-and-calibration-restructuring.md"
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Calibration struct changes and JSON format

## Description

Add `CameraPosition` dataclass and four new fields to `CameraCalibration`
(`playfield_width_cm`, `playfield_height_cm`, `camera_position`,
`tag_heights`). Add the `correct_world_for_height` method. Update
`load_calibration_from_camera_dir`, `save_calibration_to_camera_dir`, and
`load_field_dimensions_from_camera_dir` to read the new `playfield` sub-dict
format with backward-compatible fallback to old top-level keys.

This ticket is purely the data layer (`calibration.py`). No changes to
the daemon pipeline or MCP server.

## Acceptance Criteria

- [x] `CameraPosition` dataclass exists with `x_offset`, `y_offset`,
      `height` float fields defaulting to `0.0`.
- [x] `CameraCalibration` has `playfield_width_cm: float = 0.0`,
      `playfield_height_cm: float = 0.0`,
      `camera_position: Optional[CameraPosition] = None`,
      `tag_heights: dict[int, float]` (default empty dict).
- [x] `correct_world_for_height(wx, wy, h)` returns `(wx, wy)` unchanged
      when `camera_position` is `None`.
- [x] `correct_world_for_height(wx, wy, h)` returns `(wx, wy)` unchanged
      when `camera_position.height == 0.0`.
- [x] `correct_world_for_height(wx, wy, 0.0)` returns `(wx, wy)` (identity
      for h=0 regardless of camera_position.height).
- [x] `correct_world_for_height` returns correct displaced coordinates for
      a known numeric example: camera at (cx=0, cy=0, H=180), tag at
      (wx=50, wy=50), h=12 → result ≈ (46.667, 46.667), tolerance 0.01.
- [x] `load_calibration_from_camera_dir` reads `data["playfield"]["width"]`
      / `data["playfield"]["height"]` into `playfield_width_cm` /
      `playfield_height_cm`.
- [x] `load_calibration_from_camera_dir` falls back to top-level
      `field_width_cm` / `field_height_cm` when `playfield` key is absent.
- [x] `load_calibration_from_camera_dir` reads `camera_position` dict into
      `CameraPosition`; `None` when key is absent.
- [x] `load_calibration_from_camera_dir` reads `tag_heights` dict with
      string keys converted to `int`; empty dict when key is absent.
- [x] `save_calibration_to_camera_dir` writes `playfield: {width, height}`
      (new format only — no `field_width_cm` / `field_height_cm` top-level
      keys written).
- [x] `save_calibration_to_camera_dir` writes `camera_position` sub-dict
      when `cal.camera_position` is not `None`.
- [x] `save_calibration_to_camera_dir` writes `tag_heights` sub-dict.
- [x] User-managed keys in an existing `calibration.json` are preserved
      through a save cycle.
- [x] `load_field_dimensions_from_camera_dir` reads new format; falls back
      to old keys.
- [x] Round-trip test passes: save a `CameraCalibration` with
      `camera_position` and `tag_heights`, reload, verify all fields equal.
- [x] Old-format load test passes: file with `field_width_cm = 101.0` at
      top level loads with `playfield_width_cm == 101.0`.
- [x] `uv run pytest tests/ -q` passes with no regressions.

## Implementation Plan

### Approach

All changes are in `src/aprilcam/calibration/calibration.py`.
No other files are touched in this ticket.

### Files to Modify

- `src/aprilcam/calibration/calibration.py`

### Step-by-step

**Step 1**: Add `CameraPosition` immediately before `CameraCalibration`
(after the `FieldSpec` dataclass):

```python
@dataclass
class CameraPosition:
    x_offset: float = 0.0   # cm from field center, positive = right
    y_offset: float = 0.0   # cm from field center, positive = up
    height: float = 0.0     # cm above playfield
```

**Step 2**: Add four new fields to `CameraCalibration` after `pipeline`:

```python
playfield_width_cm: float = 0.0
playfield_height_cm: float = 0.0
camera_position: Optional[CameraPosition] = None
tag_heights: dict = field(default_factory=dict)  # int → float
```

Add `from dataclasses import field` import if not already present.

**Step 3**: Add `correct_world_for_height` method to `CameraCalibration`:

```python
def correct_world_for_height(
    self, wx: float, wy: float, tag_height_cm: float
) -> tuple:
    """Apply parallax correction for a tag elevated above the playfield.

    Camera at world position (x_offset, y_offset, height).
    Uncorrected homography result: (wx, wy) on z=0 plane.
    Tag is at height tag_height_cm above playfield.
    Corrected position: wx + (h/H)*(cx-wx), wy + (h/H)*(cy-wy).

    Returns (wx, wy) unchanged if camera_position is None or height is 0.
    """
    if self.camera_position is None or self.camera_position.height == 0.0:
        return wx, wy
    H = self.camera_position.height
    cx = self.camera_position.x_offset
    cy = self.camera_position.y_offset
    r = tag_height_cm / H
    return (wx + r * (cx - wx), wy + r * (cy - wy))
```

**Step 4**: Update `load_calibration_from_camera_dir` to extract new fields
after calling `CameraCalibration.from_dict(data)` (or inline):

```python
pf = data.get("playfield", {})
pw = float(pf.get("width") or data.get("field_width_cm") or 0.0)
ph = float(pf.get("height") or data.get("field_height_cm") or 0.0)
cp_dict = data.get("camera_position")
camera_position = CameraPosition(**cp_dict) if cp_dict else None
tag_heights = {int(k): float(v) for k, v in data.get("tag_heights", {}).items()}
cal = CameraCalibration.from_dict(data)
cal.playfield_width_cm = pw
cal.playfield_height_cm = ph
cal.camera_position = camera_position
cal.tag_heights = tag_heights
return cal
```

**Step 5**: Update `save_calibration_to_camera_dir`:

- Update `_CALIBRATION_KEYS` to include `"playfield"`, `"camera_position"`,
  `"tag_heights"`, `"field_width_cm"`, `"field_height_cm"` (keep old keys
  in the owned set so they are not treated as user-managed when encountered
  in old files).
- Replace `data["field_width_cm"] = ...` / `data["field_height_cm"] = ...`
  with:
  ```python
  data["playfield"] = {
      "width": field_width_cm,   # keep param names for now; ticket 002 may rename
      "height": field_height_cm,
  }
  ```
- After `data = cal.to_dict()`, add:
  ```python
  if cal.camera_position is not None:
      data["camera_position"] = {
          "x_offset": cal.camera_position.x_offset,
          "y_offset": cal.camera_position.y_offset,
          "height": cal.camera_position.height,
      }
  data["tag_heights"] = {str(k): v for k, v in cal.tag_heights.items()}
  ```

**Step 6**: Update `load_field_dimensions_from_camera_dir` with the same
fallback logic:

```python
pf = data.get("playfield", {})
w = pf.get("width") or data.get("field_width_cm")
h = pf.get("height") or data.get("field_height_cm")
if w is not None and h is not None:
    return (float(w), float(h))
```

### Testing Plan

Create `tests/test_calibration_parallax.py` (new file):

- `test_correct_world_no_position`: `camera_position=None` → identity
- `test_correct_world_zero_camera_height`: `height=0.0` → identity
- `test_correct_world_zero_tag_height`: `h=0` → identity
- `test_correct_world_formula`: camera (0,0,180), tag (50,50), h=12
  → result ≈ (46.667, 46.667)
- `test_load_new_format_playfield`: JSON with `playfield:{width:101,height:89}`
  → `playfield_width_cm=101.0`, `playfield_height_cm=89.0`
- `test_load_old_format_fallback`: JSON with `field_width_cm:101`
  → `playfield_width_cm=101.0`
- `test_load_camera_position`: `camera_position:{x_offset:0,y_offset:0,height:180}`
  → `CameraPosition(0.0, 0.0, 180.0)`
- `test_load_tag_heights`: `tag_heights:{"5": 11.8}` → `{5: 11.8}`
- `test_load_no_camera_position`: absent key → `None`
- `test_load_no_tag_heights`: absent key → `{}`
- `test_round_trip`: save with `camera_position` and `tag_heights`, reload,
  compare all new fields
- `test_old_keys_not_written`: save, read back raw JSON, verify no
  top-level `field_width_cm` key

### Documentation Updates

- Docstrings on `CameraPosition` (describe coordinate convention).
- Docstring on `correct_world_for_height` (formula derivation, edge cases).
- Update `load_calibration_from_camera_dir` docstring (new fields, fallback).
- Update `save_calibration_to_camera_dir` docstring (new format).
