---
id: '008'
title: Update daemon_cli and view_cli to use DaemonControl and stream consumers
status: done
use-cases:
  - SUC-001
  - SUC-005
depends-on:
  - '007'
github-issue: ''
issue: aprilcam-daemon-protocol-specification.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Update daemon_cli and view_cli to use DaemonControl and stream consumers

## Description

Replace all uses of `ControlClient.rpc()` and raw socket code with calls to the new
typed client API (`DaemonControl`, `ImageStreamConsumer`, `TagStreamConsumer`).

- `src/aprilcam/cli/daemon_cli.py`: replace every `ControlClient.rpc("...")` call
  with the corresponding `DaemonControl` typed method. Replace `_try_connect()` check
  (raw socket) with a gRPC connect attempt.

- `src/aprilcam/cli/view_cli.py`: remove all `socket.socket()`, `sock.connect()`, and
  `read_frame()` raw socket calls. Use `DaemonControl.get_image_stream()` and
  `DaemonControl.get_tag_stream()` to get consumers; iterate them for the view loop.

After this ticket, `src/aprilcam/daemon/client.py` (`ControlClient`, `ensure_running`)
is deleted. The `DaemonControl.connect_default(config)` method takes over the
auto-spawn responsibility.

## Acceptance Criteria

- [x] `daemon_cli.py` contains no imports from `aprilcam.daemon.client`.
- [x] `daemon_cli.py` contains no direct `socket.socket()` calls.
- [x] `aprilcam daemon start` works end-to-end via gRPC.
- [x] `aprilcam daemon status` prints running state, Unix and TCP endpoints, open cameras.
- [x] `aprilcam daemon stop` sends `DaemonControl.shutdown()` and exits cleanly.
- [x] `aprilcam daemon restart` stop + start with no errors.
- [x] `view_cli.py` contains no `socket.socket()` calls.
- [x] `view_cli.py` contains no imports from `aprilcam.daemon.client`.
- [x] `aprilcam view` renders live frames and tag overlays when a camera is open.
- [ ] `src/aprilcam/daemon/client.py` is deleted. <!-- deferred to ticket 010 per programmer instructions -->
- [x] `uv run pytest` passes.

## Implementation Plan

### `daemon_cli.py` changes

1. Replace `from aprilcam.daemon.client import ensure_running, _try_connect, ControlClient`
   with `from aprilcam.client.control import DaemonControl`.
2. `_cmd_start`: call `DaemonControl.connect_default(config)` (which calls
   `ensure_running` internally); replace `client.rpc("list_cameras")` with
   `dc.list_cameras()`.
3. `_cmd_status`: replace `_try_connect` + `ControlClient` with a `DaemonControl`
   connect attempt; replace all `.rpc(...)` calls with typed methods.
4. `_cmd_stop`: replace `ControlClient.rpc("shutdown")` with `dc.shutdown()`.
5. Update status output to print both Unix socket path and TCP port.

### `view_cli.py` changes

1. Remove all `socket`, `queue`, and `threading` imports that existed only for socket
   management.
2. Replace the socket connection block (lines ~186-204) with:
   ```python
   dc = DaemonControl.connect_default(config)
   image_consumer = dc.get_image_stream(cam_name)
   tag_consumer = dc.get_tag_stream(cam_name)
   ```
3. In the frame loop, call `image_consumer.read()` to get the numpy frame. Update the
   tag state from `tag_consumer.read()` (non-blocking or separate thread).
4. On exit, call `image_consumer.close()` and `tag_consumer.close()`.

### Files to Modify / Delete

- `src/aprilcam/cli/daemon_cli.py` — replace ControlClient usage
- `src/aprilcam/cli/view_cli.py` — replace raw socket code with consumers
- `src/aprilcam/daemon/client.py` — delete

### Testing Plan

- Manual end-to-end: `aprilcam daemon start`, `aprilcam daemon status`, `aprilcam view`,
  `aprilcam daemon stop`.
- `uv run pytest` for regression check.
- Verify no Python import warnings about missing `aprilcam.daemon.client`.
