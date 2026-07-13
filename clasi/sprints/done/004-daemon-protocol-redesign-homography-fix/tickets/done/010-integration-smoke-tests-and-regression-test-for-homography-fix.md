---
id: '010'
title: Integration smoke tests and regression test for homography fix
status: done
use-cases:
  - SUC-001
  - SUC-002
  - SUC-006
  - SUC-008
depends-on:
  - '008'
  - '009'
github-issue: ''
issue: ''
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Integration smoke tests and regression test for homography fix

## Description

Write the final validation tests for Sprint 004:

1. **Playfield homography regression test**: verifies that ticket 001's fix actually
   loads a homography matrix when a calibration file is present. This is the primary
   bug-prevention guard.

2. **gRPC daemon smoke test**: starts a real `DaemonServer` (in-process or subprocess,
   Unix-only transport, non-default socket path) and makes live gRPC calls via
   `DaemonControl`. Validates the end-to-end stack with no camera hardware required.

3. **gRPC reflection test**: verifies that the Server Reflection service is active and
   returns the expected service name.

These tests do not require a real camera. They use in-process server startup and mocked
or absent camera hardware to test the protocol layer.

## Acceptance Criteria

- [ ] `tests/test_playfield_homography.py` exists and passes:
      - With a mock calibration file present, `playfield._homography` is a 3x3 ndarray
        after `start()`.
      - With no calibration file, `playfield._homography` is None.
- [ ] `tests/test_grpc_smoke.py` exists and passes:
      - Daemon starts on an in-process gRPC server (Unix socket, temp path).
      - `DaemonControl.list_cameras()` returns an empty list.
      - `DaemonControl.shutdown()` succeeds.
      - Server terminates cleanly within the test.
- [ ] `tests/test_grpc_reflection.py` exists and passes:
      - gRPC reflection service returns `aprilcam.AprilCam` in the service list.
- [ ] All three new test files pass under `uv run pytest`.
- [ ] No existing tests are broken.

## Implementation Plan

### `tests/test_playfield_homography.py`

```python
def test_auto_discover_homography_loads_calibration(tmp_path, monkeypatch):
    # Write a mock calibration.json to tmp_path
    calibration = {"cameras": {"test-cam": {"homography": [[1,0,0],[0,1,0],[0,0,1]]}}}
    (tmp_path / "calibration.json").write_text(json.dumps(calibration))

    # Monkeypatch Playfield so it uses tmp_path as data_dir and mocks the camera open
    # Assert playfield._homography is np.array([[1,0,0],[0,1,0],[0,0,1]])

def test_auto_discover_homography_returns_none_when_no_file(tmp_path):
    # No calibration file present
    # Assert playfield._homography is None after start()
```

The test mocks `cv2.VideoCapture` to return a minimal fake capture object that
reports a known device name and resolution.

### `tests/test_grpc_smoke.py`

```python
def test_list_cameras_via_daemon_control(tmp_path):
    # Build a DaemonServer with unix_enabled=True, tcp_enabled=False, unix_path=tmp_path/...
    # Start it in a background thread
    # Connect DaemonControl to the Unix socket
    # Assert list_cameras() == []
    # Call shutdown()
    # Join server thread with timeout
```

Uses `threading.Thread` with a short-lived gRPC server. No camera hardware touched.

### `tests/test_grpc_reflection.py`

```python
def test_reflection_service_lists_aprilcam(tmp_path):
    # Start daemon as above
    # Use grpc_reflection client to list services
    # Assert "aprilcam.AprilCam" in service list
```

### Files to Create

- `tests/test_playfield_homography.py`
- `tests/test_grpc_smoke.py`
- `tests/test_grpc_reflection.py`

### Testing Plan

- Run `uv run pytest tests/test_playfield_homography.py tests/test_grpc_smoke.py
  tests/test_grpc_reflection.py -v`.
- Run full `uv run pytest` to ensure no regressions.
