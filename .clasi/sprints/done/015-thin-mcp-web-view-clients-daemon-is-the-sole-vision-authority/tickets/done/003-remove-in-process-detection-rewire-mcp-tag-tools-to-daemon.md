---
id: '003'
title: Remove in-process detection; rewire MCP tag tools to daemon
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
depends-on:
- '001'
github-issue: ''
issue: ''
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Remove in-process detection; rewire MCP tag tools to daemon

## Description

Remove the in-process detection machinery from `mcp_server.py` — `DaemonCapture`,
`DetectionLoop`/`AprilCam` instantiation, `resolve_source`, `_resolve_source_playfield`,
and the `DetectionEntry` dataclass — then rewire all tag/detection MCP tools to use
daemon RPCs instead.

After this ticket:
- `stream_tags` / `start_detection` subscribe to the daemon's `GetTagStream` socket;
  tag records (plain dicts) accumulate in a `deque` (maxlen=300).
- `get_tags` reads the latest tag-record dict from the deque.
- `get_tag_history` returns the last N dicts from the deque.
- `get_objects` calls `client.get_objects()` (the RPC from ticket 001).
- `stop_stream` / `stop_detection` closes the `TagStreamConsumer`.
- `get_frame` / `capture_frame` remain as-is (already pass JPEG bytes from daemon).
- Gripper world-xy computation stays in the MCP (pure dict math, no cv2).
- No `import cv2` remains in `mcp_server.py`.

Depends on ticket 001 (the `GetObjects` RPC must exist before `_handle_get_objects`
can call `client.get_objects()`).

## Acceptance Criteria

- [x] `mcp_server.py` contains no class definition for `DaemonCapture`.
- [x] `mcp_server.py` contains no import of `DetectionLoop`, `AprilCam`, or
  `RingBuffer` from `aprilcam.core.detection`.
- [x] `mcp_server.py` contains no `resolve_source()` function and no
  `_resolve_source_playfield()` function.
- [x] `mcp_server.py` contains no `import cv2` (at module level or in any
  function — including inline imports).
- [x] `stream_tags` and `start_detection` start a `TagStreamConsumer` (from
  `client/stream.py`) connected to the daemon's tag stream socket; they do NOT
  instantiate `DetectionLoop`, `AprilCam`, or `DaemonCapture`.
- [x] A new `DaemonStreamEntry` dataclass (or equivalent) holds: `source_id`,
  `consumer: TagStreamConsumer`, `history: deque[dict]`, `robot_tag_id`,
  `gripper_offset_cm`, `_camera_id`.
- [x] `get_tags` reads the latest `TagFrame` dict from the deque; includes
  `gripper_world_xy` computed via `_compute_gripper_world_xy()` when `robot_tag_id`
  is set (this function operates on dict fields; no cv2 needed).
- [x] `get_tag_history` returns the last N dicts from the deque.
- [x] `_handle_get_objects` calls `client.get_objects(cam_name)` and returns the
  structured result; no cv2, no `ColorClassifier`, no numpy pixel ops.
- [x] `stop_stream` / `stop_detection` closes the `TagStreamConsumer` and removes
  the `DaemonStreamEntry` from the registry.
- [x] `uv run pytest tests/test_mcp_*.py` green (update tests that mock the old
  `DetectionEntry` to mock `DaemonStreamEntry` instead).
- [x] `uv run pytest` (full suite) green.

## Implementation Plan

### Step 1: Introduce `DaemonStreamEntry`

Add a new dataclass (or plain class) to replace `DetectionEntry`:

```python
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class DaemonStreamEntry:
    source_id: str
    consumer: "TagStreamConsumer"        # from client/stream.py
    history: deque = field(default_factory=lambda: deque(maxlen=300))
    robot_tag_id: Optional[int] = None
    gripper_offset_cm: float = 14.0
    _camera_id: Optional[str] = None
```

Replace `detection_registry: dict[str, DetectionEntry]` with
`detection_registry: dict[str, DaemonStreamEntry]`.

### Step 2: Rewrite `_handle_start_detection` and `stream_tags`

Replace the `DaemonCapture` + `DetectionLoop` + `AprilCam` instantiation block with:

```python
from aprilcam.client.stream import TagStreamConsumer

client = _ensure_daemon_client()
endpoint = client.get_tag_stream(cam_name)   # GetTagStream RPC → StreamEndpoint
consumer = TagStreamConsumer(host=..., port=endpoint.tcp_port or ...,
                             socket_path=endpoint.socket_path or None)
consumer.start()   # starts background thread pushing TagFrame dicts into queue

entry = DaemonStreamEntry(
    source_id=source_id,
    consumer=consumer,
    robot_tag_id=robot_tag_id,
    gripper_offset_cm=gripper_offset_cm,
    _camera_id=camera_id,
)
detection_registry[source_id] = entry
```

The `TagStreamConsumer` background thread must call a callback (or populate the
`entry.history` deque) each time a new `TagFrame` arrives. Wire this up so that
`history.appendleft(tag_frame_dict)` is called on each received frame.

### Step 3: Rewrite `get_tags` / `_handle_get_tags`

```python
def _handle_get_tags(source_id: str) -> dict:
    entry = detection_registry.get(source_id)
    if entry is None:
        # Fall back: call GetTags RPC directly (no active stream)
        client = _ensure_daemon_client()
        resp = client.get_tags(source_id)
        return _tag_frame_response_to_dict(resp, source_id)
    latest = entry.history[0] if entry.history else None
    if latest is None:
        return {"source_id": source_id, "frame": None, "tags": []}
    result = dict(latest)
    result["source_id"] = source_id
    if entry.robot_tag_id is not None:
        _inject_gripper_world_xy(result, entry)  # pure dict math
    return result
```

### Step 4: Rewrite `get_tag_history`

```python
def _handle_get_tag_history(source_id: str, num_frames: int = 30) -> dict:
    entry = detection_registry.get(source_id)
    if entry is None:
        return {"error": f"No detection running on '{source_id}'"}
    frames = list(entry.history)[:num_frames]
    return {"source_id": source_id, "frames": frames}
```

### Step 5: Rewrite `_handle_get_objects`

```python
def _handle_get_objects(source_id: str) -> dict:
    try:
        client = _ensure_daemon_client()
        cam_name = _resolve_cam_name(source_id)   # from registry
        resp = client.get_objects(cam_name)
        return {
            "source_id": source_id,
            "objects": [
                {
                    "center_px": [o.cx_px, o.cy_px],
                    "world_xy": [o.wx, o.wy] if (o.wx or o.wy) else None,
                    "color": o.color,
                    "bbox": [o.x_bbox, o.y_bbox, o.w_bbox, o.h_bbox],
                    "area_px": o.area_px,
                    "object_type": o.object_type,
                    "confidence": o.confidence,
                }
                for o in resp.objects
            ],
        }
    except Exception as exc:
        return {"error": f"Unexpected error: {exc}"}
```

### Step 6: Rewrite `stop_stream` / `stop_detection`

```python
def _handle_stop_stream(source_id: str) -> dict:
    entry = detection_registry.pop(source_id, None)
    if entry is None:
        return {"error": f"No stream on '{source_id}'"}
    entry.consumer.stop()
    return {"status": "stopped", "source_id": source_id}
```

### Step 7: Delete dead code

After completing the above:
- Delete `DaemonCapture` class (~lines 209–237).
- Delete `resolve_source()` function (~line 429).
- Delete `_resolve_source_playfield()` function (~line 184).
- Delete `DetectionEntry` dataclass.
- Remove `from aprilcam.core.detection import DetectionLoop, RingBuffer` import.
- Remove `from aprilcam.core.aprilcam import AprilCam` import (if only used by deleted code).
- Remove `import cv2` / `import numpy as np` at module level if no longer needed.
  (Verify: `np` may still be used in `_compute_gripper_world_xy` or homography math
  — check before removing.)

### Gripper world-xy (`_compute_gripper_world_xy`)

This function operates on tag dict fields + a homography matrix (9 floats). It may
use `numpy` for matrix multiply. Keep it. Remove any `cv2` usage within it if any
exists (grep for `cv2` in that function specifically).

### A1 coord transforms

`_a1_coord_transform`, `_get_playfield_origin` — keep. These operate on scalars
from the playfield registry (no pixels, no cv2).

### Testing

- Update `tests/test_mcp_*.py` files that reference `DetectionEntry`, `DaemonCapture`,
  `DetectionLoop`, or `RingBuffer` from the MCP perspective — mock `DaemonStreamEntry`
  and `TagStreamConsumer` instead.
- Write `tests/test_015_003_mcp_tag_tools_daemon.py` with:
  - Test: `stream_tags` starts a `TagStreamConsumer` (mock the consumer; assert
    `get_tag_stream` RPC was called).
  - Test: `get_tags` returns the latest dict from the deque.
  - Test: `get_tag_history` returns the correct slice.
  - Test: `get_objects` calls `client.get_objects()` and maps the response.

### Files to modify/create

- `src/aprilcam/server/mcp_server.py` — full rework (see steps 1-7)
- `tests/test_015_003_mcp_tag_tools_daemon.py` — new test file
- `tests/test_mcp_*.py` — update mocks as needed
