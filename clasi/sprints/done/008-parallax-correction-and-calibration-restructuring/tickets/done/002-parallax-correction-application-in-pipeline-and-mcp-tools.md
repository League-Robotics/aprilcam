---
id: "002"
title: "Parallax correction application in pipeline and MCP tools"
status: done
use-cases:
  - SUC-003
depends-on:
  - "001"
github-issue: ""
issue: "parallax-correction-and-calibration-restructuring.md"
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Parallax correction application in pipeline and MCP tools

## Description

Wire the parallax correction (built in ticket 001) into the two places
where world coordinates are consumed:

1. **Daemon detection pipeline** (`camera_pipeline.py`): after computing
   `world_xy` for each tag, apply `correct_world_for_height` using
   `calibration.tag_heights`. This makes the correction automatic for all
   streaming consumers.

2. **`calibrate_playfield` MCP tool**: add `camera_height_cm`,
   `camera_x_offset_cm`, `camera_y_offset_cm` parameters so agents can
   record camera position in one call.

3. **`get_tags` MCP tool**: add optional `tag_heights_json` parameter for
   per-call height override. The JSON dict merges over the persisted
   `calibration.tag_heights` for that call only.

Depends on ticket 001 for `CameraPosition`, `correct_world_for_height`,
and the new `tag_heights` / `camera_position` fields on `CameraCalibration`.

## Acceptance Criteria

### Daemon pipeline

- [x] After computing `world_xy` for a tag, the pipeline checks
      `self._calibration.tag_heights.get(tr.id, 0.0)`.
- [x] If `tag_h > 0.0` and `tr.world_xy is not None` and
      `self._calibration.camera_position` is set, calls
      `correct_world_for_height` and assigns the result back to
      `tr.world_xy`.
- [x] Tags whose IDs are not in `tag_heights` are unaffected.
- [x] When `calibration` is `None` or `camera_position` is `None`, the
      block is skipped entirely (no exception).

### `calibrate_playfield` MCP tool

- [x] Accepts `camera_height_cm: float` (no default — caller must supply).
- [x] Accepts `camera_x_offset_cm: float = 0.0`.
- [x] Accepts `camera_y_offset_cm: float = 0.0`.
- [x] Constructs `CameraPosition(x_offset, y_offset, camera_height_cm)`
      and stores it on the `CameraCalibration` before saving.
- [x] Saved `calibration.json` contains `camera_position` sub-dict with
      the supplied values.
- [x] Existing calls without the new parameters are not broken (default
      values apply; `camera_position` is stored with height=0 if omitted).
- [x] Response JSON includes `camera_height_cm` confirming the stored value.

### `get_tags` MCP tool

- [x] Accepts optional `tag_heights_json: str | None = None`.
- [x] When `None`, behavior is unchanged from current.
- [x] When provided, parses as JSON dict (string keys → int tag IDs, float values).
- [x] Merges over `calibration.tag_heights` (per-call values take precedence
      for matching IDs; other IDs from calibration remain).
- [x] Applies `correct_world_for_height` to each tag's `world_xy` using
      the merged heights when `world_xy` is not None.
- [x] `tag_heights_json='{"5": 0}'` results in no correction for tag 5
      (zero height → identity).
- [x] Tags not in the merged dict are not corrected.
- [x] Invalid JSON in `tag_heights_json` returns `{"error": "..."}`.
- [x] `world_xy` values in the response reflect the corrected coordinates.

### General

- [x] `uv run pytest tests/ -q` passes with no regressions.
- [x] Docstrings on `calibrate_playfield` and `get_tags` updated to document
      new parameters and their effect.

## Implementation Plan

### Approach

Three targeted changes in two files:
- `src/aprilcam/daemon/camera_pipeline.py`: 5-line correction block.
- `src/aprilcam/server/mcp_server.py`: two tool function signatures updated.

### Files to Modify

- `src/aprilcam/daemon/camera_pipeline.py`
- `src/aprilcam/server/mcp_server.py`

### camera_pipeline.py

Locate the section in the detection loop where `world_xy` is computed via
the homography. This is in `AprilTag.update()` or `AprilTag.from_detection()`
inside `core/models.py`, but the pipeline has access to the tag object
after the frame is processed. The mutable `AprilTag` objects in the
tracker's state are updated in place.

Find the per-tag iteration in `camera_pipeline.py` (the loop that calls
`get_objects()` or iterates over `tracker.tags`). After the frame is
processed and tags have been updated with homography, insert:

```python
if self._calibration and self._calibration.camera_position:
    for tr in current_tags:   # whatever the iteration variable is
        tag_h = self._calibration.tag_heights.get(tr.id, 0.0)
        if tag_h > 0.0 and tr.world_xy is not None:
            tr.world_xy = self._calibration.correct_world_for_height(
                tr.world_xy[0], tr.world_xy[1], tag_h
            )
```

The exact integration point requires reading the camera_pipeline.py
detection loop to find the right place (after homography is applied to
tags, before tags are written to the ring buffer). The programmer should
read `camera_pipeline.py` in full to identify the exact insertion point.

### mcp_server.py — calibrate_playfield

Add three parameters to the function signature:

```python
async def calibrate_playfield(
    playfield_id: str,
    width: float,
    height: float,
    units: str = "inch",
    camera_height_cm: float = 0.0,
    camera_x_offset_cm: float = 0.0,
    camera_y_offset_cm: float = 0.0,
) -> list[TextContent]:
```

After the homography is computed and before saving, construct and attach
the `CameraPosition`:

```python
from ..calibration.calibration import CameraPosition  # add to imports
# ...
cal.camera_position = CameraPosition(
    x_offset=camera_x_offset_cm,
    y_offset=camera_y_offset_cm,
    height=camera_height_cm,
)
```

Update the success response to include `"camera_height_cm": camera_height_cm`.

Update docstring to document the three new parameters.

### mcp_server.py — get_tags

Add the parameter to the function signature:

```python
async def get_tags(
    source_id: str,
    tag_heights_json: str | None = None,
) -> list[TextContent]:
```

In the handler body, after retrieving tags from `_handle_get_tags`,
apply the override if provided:

```python
if tag_heights_json is not None:
    try:
        override = {int(k): float(v) for k, v in json.loads(tag_heights_json).items()}
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        return [TextContent(type="text", text=json.dumps({"error": f"Invalid tag_heights_json: {exc}"}))]

    # Get calibration for this source
    calibration = _get_calibration_for_source(source_id)  # existing helper or inline
    if calibration and calibration.camera_position:
        merged = {**calibration.tag_heights, **override}
        for tag in result.get("tags", []):
            tag_h = merged.get(tag["id"], 0.0)
            if tag_h > 0.0 and tag.get("world_xy"):
                tag["world_xy"] = list(
                    calibration.correct_world_for_height(
                        tag["world_xy"][0], tag["world_xy"][1], tag_h
                    )
                )
```

The programmer should read `mcp_server.py` to find the existing pattern
for accessing calibration from a source_id — there may already be a
helper that retrieves the calibration object from the playfield registry.
Use that pattern rather than duplicating the lookup.

Update docstring on `get_tags` to document `tag_heights_json`.

### Testing Plan

The correction math is already tested in ticket 001. This ticket's tests
focus on integration points.

Add to `tests/test_calibration_parallax.py` or create
`tests/test_parallax_integration.py`:

- `test_get_tags_tag_heights_json_overrides`: mock the get_tags path with
  a known `world_xy` and a `tag_heights_json` override; verify corrected
  coordinates in response.
- `test_get_tags_tag_heights_json_zero_suppresses`: `{"5": 0}` → no
  correction for tag 5.
- `test_get_tags_invalid_json_returns_error`: malformed JSON string → error.
- `test_get_tags_no_override_unchanged`: `tag_heights_json=None` → same
  result as before.

For `calibrate_playfield`:
- `test_calibrate_playfield_stores_camera_position`: after calibration,
  reload `calibration.json` and verify `camera_position.height` matches
  the supplied value.

### Documentation Updates

- `calibrate_playfield` docstring: describe `camera_height_cm`,
  `camera_x_offset_cm`, `camera_y_offset_cm`; note that `camera_position`
  is stored in `calibration.json` for use by the daemon pipeline.
- `get_tags` docstring: describe `tag_heights_json` format, merge behavior,
  and note that `{"id": 0}` suppresses correction for that tag.
- Note in streaming tool docstrings (`stream_tags`, `start_detection`)
  that daemon applies persistent `tag_heights` automatically; to change
  heights at runtime, update `calibration.json` and call `reload_calibration`.
