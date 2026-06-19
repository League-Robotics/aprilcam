---
id: 014-008
title: Unit and integration tests, verification grep, and version bump
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
- SUC-005
depends-on:
- 014-002
- 014-003
- 014-004
- 014-005
- 014-006
- 014-007
---

# 014-008: Unit and integration tests, verification grep, and version bump

## Description

Consolidate and complete the test suite for this sprint. Prior tickets each
specify their own tests; this ticket adds any missing coverage, runs the
verification greps from the issue's Verification section, and bumps the
version.

## Acceptance Criteria

- [x] `tests/test_discovery.py` covers all four resolver branches (explicit,
      env/config, local unix, mDNS 0/1/N). Already added in ticket 003; verify
      it is complete here.
- [x] `tests/test_daemon_control_no_spawn.py` (or added to existing tests):
      `DaemonControl.connect_default` with an unreachable target raises
      `DaemonNotFoundError` and does NOT call `subprocess.Popen`.
- [x] `tests/test_file_proxy_rpcs.py`: round-trip tests for each file-proxy
      RPC handler (GetCalibration/SetCalibration, GetCameraConfig/SetCameraConfig,
      GetPaths/SetPaths, ListPlayfields) using a temp directory — no live daemon
      required.
- [x] `tests/test_connect_daemon.py` (or added to existing): mock-based test
      that `connect_daemon()` tears down session state before reconnecting
      (checks that all registry clear methods are called).
- [x] `tests/test_stream_consumer_host.py` (or added to existing): assert
      `ImageStreamConsumer` uses the provided `host` for TCP connect
      (mock `socket.connect`).
- [x] **Verification greps (all must pass):**
  - `grep -r "VideoCapture" src/aprilcam/ --include="*.py" | grep -v "camera_pipeline.py"` → zero output.
  - `grep -r "AF_UNIX" src/aprilcam/server/ --include="*.py"` → zero output.
  - `grep -r "subprocess.Popen" src/aprilcam/client/control.py` → zero output.
  - `grep -rn "camera_dir\|paths_file" src/aprilcam/server/mcp_server.py | grep -v "^\s*#"` → zero output.
  - `grep -n "camutil.list_cameras\|from.*camutil.*import.*list_cameras" src/aprilcam/server/mcp_server.py src/aprilcam/cli/cameras_cli.py src/aprilcam/cli/calibrate_cli.py` → zero output.
- [x] `uv run pytest` passes with exit code 0.
- [x] Version bumped in `pyproject.toml` per project convention
      (`0.YYYYMMDD.N` where N increments from the last build today).

## Implementation Plan

### Test approach

Most tests are unit tests using `unittest.mock` to avoid needing a live daemon.
The `test_file_proxy_rpcs.py` tests use a `tmp_path` fixture and call handler
methods directly on an `AprilCamServicer` instance with a fake `_cameras` dict.

### `test_file_proxy_rpcs.py` sketch

```python
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock
from aprilcam.daemon.grpc_server import AprilCamServicer

def test_get_set_calibration(tmp_path):
    # Setup: write a known calibration.json
    cam_dir = tmp_path / "cameras" / "test-cam"
    cam_dir.mkdir(parents=True)
    cal_data = {"homography": [[1, 0, 0], [0, 1, 0], [0, 0, 1]]}
    (cam_dir / "calibration.json").write_text(json.dumps(cal_data))

    servicer = _make_servicer(tmp_path)
    # ... call GetCalibration, assert blob matches
    # ... call SetCalibration with new data, assert file updated
```

### Version bump

After all tests pass, run:
```bash
dotconfig version bump
```
or manually update `pyproject.toml` `version = "0.YYYYMMDD.N"` to today's
date with next N. Commit with message `chore: bump version`.

### Files to Create/Modify

- `tests/test_discovery.py` — verify / complete (added in ticket 003).
- `tests/test_daemon_control_no_spawn.py` — new.
- `tests/test_file_proxy_rpcs.py` — new.
- `tests/test_connect_daemon.py` — new.
- `tests/test_stream_consumer_host.py` — new.
- `pyproject.toml` — version bump.

### Testing Plan

Run `uv run pytest -v` and verify all tests pass. Run each verification grep
and confirm zero output. Record the results in the commit message.
