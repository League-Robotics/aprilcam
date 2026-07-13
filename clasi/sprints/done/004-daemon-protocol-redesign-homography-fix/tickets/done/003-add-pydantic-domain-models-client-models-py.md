---
id: '003'
title: Add Pydantic domain models (client/models.py)
status: done
use-cases:
  - SUC-002
  - SUC-003
  - SUC-004
  - SUC-005
depends-on:
  - '002'
github-issue: ''
issue: aprilcam-daemon-protocol-specification.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Add Pydantic domain models (client/models.py)

## Description

Create the `aprilcam.client` package and the `models.py` module containing typed
Pydantic models for all data that application code works with after deserialization.
These models replace the untyped `dict` objects that currently come back from
`ControlClient.rpc()` calls and from parsing `FrameMessage.tags`.

The proto-generated `*_pb2` types are used only inside adapter code (consumers and
`DaemonControl`). All application code — CLI, MCP tools, tests — receives Pydantic
models only.

## Acceptance Criteria

- [ ] `src/aprilcam/client/__init__.py` exists.
- [ ] `src/aprilcam/client/models.py` defines `TagRecord`, `TagFrame`, `CameraInfo`,
      `PathRecord` as Pydantic `BaseModel` subclasses with the field types specified
      in architecture-update.md Section 2.8.
- [ ] `TagRecord.world_xy` is `tuple[float, float] | None` (None when uncalibrated).
- [ ] `TagFrame.homography` is `list[list[float]] | None` (3x3 or None).
- [ ] `CameraInfo.frame_size` is `tuple[int, int]`.
- [ ] All models are importable: `from aprilcam.client.models import TagFrame`.
- [ ] Unit tests for model construction pass.

## Implementation Plan

### Approach

1. Create `src/aprilcam/client/__init__.py` (empty or minimal exports).
2. Create `src/aprilcam/client/models.py` with all four Pydantic models exactly as
   specified in architecture-update.md Section 2.8:
   - `TagRecord` — id, center_px, corners_px, yaw, world_xy, in_playfield, vel_px,
     speed_px, vel_world, speed_world, heading_rad, age
   - `TagFrame` — frame_id, ts_mono_ns, ts_wall_ms, tags, homography, playfield_corners,
     fps
   - `CameraInfo` — cam_name, calibrated, frame_size, fps
   - `PathRecord` — points, color, thickness, closed

### Files to Create

- `src/aprilcam/client/__init__.py`
- `src/aprilcam/client/models.py`

### Testing Plan

- Write `tests/test_client_models.py`:
  - Construct a `TagRecord` with all required fields; verify field types.
  - Construct a `TagFrame` with two `TagRecord` instances; verify `len(frame.tags) == 2`.
  - Construct `TagFrame` with `homography=None`; verify it is accepted.
  - Construct `CameraInfo`; verify `frame_size` is a tuple of two ints.
  - Verify Pydantic validation raises on missing required fields.
- Run `uv run pytest tests/test_client_models.py`.
