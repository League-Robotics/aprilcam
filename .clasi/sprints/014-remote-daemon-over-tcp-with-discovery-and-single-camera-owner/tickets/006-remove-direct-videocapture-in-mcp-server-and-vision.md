---
id: '014-006'
title: Remove direct VideoCapture in MCP server and vision/objects.py
status: open
use-cases:
  - SUC-002
depends-on:
  - 014-003
---

# 014-006: Remove direct VideoCapture in MCP server and vision/objects.py

## Description

Remove all `cv2.VideoCapture(camera_index)` calls from the MCP server's
`cam_<index>` branches and from `vision/objects.py`. After this ticket,
`grep -r "VideoCapture" src/aprilcam/ --include="*.py"` hits only
`daemon/camera_pipeline.py` (the one sanctioned site).

This ticket also removes `_frames_from_daemon` (the AF_UNIX frame-fetch path)
and replaces it with `client.capture_frame()` gRPC calls throughout the MCP
server's one-shot frame-fetch paths.

## Acceptance Criteria

- [ ] `server/mcp_server.py` `start_detection` no longer contains a
      `cv2.VideoCapture(camera_index)` call (lines ~1104, ~1111). The
      `cam_<index>` exclusive-capture branch is deleted. All detection sources
      use `DaemonCapture`.
- [ ] `server/mcp_server.py` `stream_tags` no longer contains a
      `cv2.VideoCapture(camera_index)` call (lines ~2769, ~2775). Same removal.
- [ ] `stop_detection` and `stop_stream` teardown code that re-opens
      `cv2.VideoCapture` for re-sync (lines ~1192, ~2876) is deleted.
- [ ] `_frames_from_daemon` (AF_UNIX `socket.AF_UNIX` path, line ~779–820)
      is deleted or replaced. All callers of `_frames_from_daemon` in
      `resolve_source()` / `_read_one_frame()` now call
      `_ensure_daemon_client().capture_frame(cam_name)` instead.
- [ ] `vision/objects.py` `ObjectTracker.start()` does not call
      `cv.VideoCapture(self._camera_index)`. Either:
      (a) `ObjectTracker` is removed/refactored to consume from the daemon
          ring buffer instead, or
      (b) A `# DAEMON-ONLY` docstring guard is added and the class is gated
          so no MCP/CLI entry point can instantiate it with a device index.
      Document the chosen approach in code comments.
- [ ] `grep -r "VideoCapture" src/aprilcam/ --include="*.py"` returns only
      lines from `daemon/camera_pipeline.py`.
- [ ] `grep -r "AF_UNIX" src/aprilcam/server/ --include="*.py"` returns zero
      matches.
- [ ] `uv run pytest` passes.

## Implementation Plan

### Approach

Work through `mcp_server.py` first (the largest change), then `vision/objects.py`.

### MCP server changes

**`start_detection` (~1104):**
- Find the `if cam_idx is not None:` block that does `exclusive_cap = cv2.VideoCapture(camera_index)`.
- Delete this block entirely. All `cam_<index>` handles are now illegal; return
  an error if a caller passes one: "direct camera index handles are no longer
  supported; use open_camera() to get a daemon-owned camera handle."
- The remaining path (daemon-owned cameras → `DaemonCapture`) is the only path.

**`stream_tags` (~2769):** same deletion.

**`stop_detection` / `stop_stream` teardown (~1192, ~2876):** delete the
`shared_cap = cv2.VideoCapture(camera_index)` re-open blocks. These were only
needed to release a locally-held camera; daemon-owned cameras need no client-side
release.

**`_frames_from_daemon` / `_read_one_frame`:** replace with:
```python
def _read_one_frame(camera_id: str) -> np.ndarray | None:
    """Fetch a single frame from the daemon via gRPC CaptureFrame."""
    client = _ensure_daemon_client()
    info = _cam_info.get(camera_id)
    cam_name = info.get("cam_name") if info else camera_id
    frame = client.capture_frame(cam_name)  # returns ImageFrame Pydantic model
    if frame is None:
        return None
    return frame.image  # numpy array
```
Update `resolve_source()` and all `_frames_from_daemon` call sites to use
`_read_one_frame`.

### `vision/objects.py` changes

Audit: does any MCP tool (`get_objects`) call `ObjectTracker.__init__` with
a device index? If yes, refactor `get_objects` to feed frames from the daemon
ring buffer rather than opening a new camera.

If `ObjectTracker` is only used from daemon-internal code or is not reached
from any MCP tool handler, add a docstring:
```python
class ObjectTracker:
    """DAEMON-ONLY — opens a VideoCapture device directly.
    Not reachable from MCP server or CLI client entry points.
    """
```
and add a runtime guard:
```python
def start(self):
    # Enforce daemon-only invariant
    import aprilcam.daemon  # raises ImportError if not in daemon context
    self._cap = cv.VideoCapture(self._camera_index)
    ...
```
(The exact guard mechanism depends on how the code is structured; the key
requirement is that no MCP tool can trigger this code path.)

### Legacy opener audit

Audit the following files and add `# DAEMON-ONLY` or `# DEAD-CODE` comments
at any `VideoCapture` site that is daemon-internal or unreachable:
- `core/aprilcam.py` (~478, ~696)
- `calibration/calibration.py` (~1552)
- `camera/camera.py`
- `camera/video_camera.py`

None of these should be removed in this ticket (they may be needed by the
daemon itself); just confirm and document.

### Testing Plan

- `uv run pytest` full suite.
- `grep -r "VideoCapture" src/aprilcam/ --include="*.py"` — review output,
  confirm only `daemon/camera_pipeline.py` has device-index opens.
- Manually test: with daemon running, call `start_detection` and `get_tags`
  via the MCP interface. Confirm tags arrive without a local camera opening.
- Confirm `capture_frame` MCP tool works against a locally-running daemon.

### Documentation Updates

- Update `AGENT_GUIDE.md` note: "`cam_<index>` handles are no longer accepted;
  use `open_camera()` to get a daemon handle."
