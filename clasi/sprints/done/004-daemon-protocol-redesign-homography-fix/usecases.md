---
status: final
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 004 Use Cases

## SUC-001: Daemon Lifecycle via gRPC CLI

- **Actor**: Developer / operator running `aprilcam daemon` commands
- **Preconditions**: `aprilcam` is installed; no daemon is running
- **Main Flow**:
  1. Operator runs `aprilcam daemon start`.
  2. CLI calls `DaemonControl.connect_default(config)`, which spawns the daemon if not running.
  3. Daemon binds gRPC on both Unix socket and TCP port 5280.
  4. CLI calls `list_cameras()` and prints summary.
  5. Operator runs `aprilcam daemon status` — CLI shows running, transport endpoints, open cameras.
  6. Operator runs `aprilcam daemon stop` — CLI calls `DaemonControl.shutdown()`.
  7. Daemon exits cleanly; all stream sockets are closed.
- **Postconditions**: Daemon is stopped; pidfile is removed; all sockets cleaned up.
- **Acceptance Criteria**:
  - [ ] `aprilcam daemon start` spawns daemon when not running.
  - [ ] `aprilcam daemon status` reports Unix and TCP endpoints.
  - [ ] `aprilcam daemon stop` terminates daemon without errors.
  - [ ] `aprilcam daemon restart` stops then starts cleanly.
  - [ ] `--no-unix` flag disables Unix socket; `--no-tcp` disables TCP.
  - [ ] `--unix` + `--no-tcp` combination works; daemon-only-unix mode.
  - [ ] `--no-unix --no-tcp` exits with error message.

## SUC-002: Camera Open and gRPC Query

- **Actor**: Client code or CLI using `DaemonControl`
- **Preconditions**: Daemon is running; a camera is available at index 0
- **Main Flow**:
  1. Client creates `DaemonControl` and connects to the daemon.
  2. Client calls `open_camera(0)` — daemon starts the pipeline and returns `cam_name`.
  3. Client calls `get_camera_info(cam_name)` — receives a `CameraInfo` Pydantic model.
  4. Client calls `capture_frame(cam_name)` — receives a decoded `np.ndarray`.
  5. Client calls `get_tags(cam_name)` — receives a `TagFrame` Pydantic model.
  6. Client calls `close_camera(cam_name)` — pipeline stops; stream sockets removed.
- **Postconditions**: Camera is closed; no resources leaked.
- **Acceptance Criteria**:
  - [ ] `DaemonControl.open_camera(0)` returns a cam_name string.
  - [ ] `DaemonControl.get_camera_info(cam_name)` returns a `CameraInfo` with correct fields.
  - [ ] `DaemonControl.capture_frame(cam_name)` returns an `np.ndarray` (BGR, height > 0).
  - [ ] `DaemonControl.get_tags(cam_name)` returns a `TagFrame` with frame_id and fps fields.
  - [ ] `DaemonControl.close_camera(cam_name)` succeeds without exceptions.

## SUC-003: Tag Stream Subscription

- **Actor**: Client code that needs continuous tag data (e.g., `view_cli.py`)
- **Preconditions**: Daemon running; camera open; `DaemonControl` connected
- **Main Flow**:
  1. Client calls `DaemonControl.get_tag_stream(cam_name, max_hz=20)`.
  2. Daemon creates a `TagStreamProducer`, allocates stream endpoint, returns it.
  3. `DaemonControl` constructs and connects a `TagStreamConsumer` and returns it.
  4. Client iterates the consumer: receives `TagFrame` objects on each change.
  5. When no tags move more than 8 px, client receives a heartbeat frame after 1s.
  6. Client calls `consumer.close()` — connection closes; producer cleans up.
- **Postconditions**: Stream is closed; no background threads remain for this subscription.
- **Acceptance Criteria**:
  - [ ] `TagStreamConsumer` iterates and yields `TagFrame` Pydantic objects.
  - [ ] Adaptive publish: no publish when tags are stationary beyond 8 px threshold.
  - [ ] Heartbeat published at 1s intervals when no change.
  - [ ] `max_hz` parameter limits publish rate to the requested cap.
  - [ ] Consumer can be created for both Unix socket and TCP endpoints.

## SUC-004: Image Stream Subscription

- **Actor**: Client code that needs a live video feed (e.g., `view_cli.py`)
- **Preconditions**: Daemon running; camera open; `DaemonControl` connected
- **Main Flow**:
  1. Client calls `DaemonControl.get_image_stream(cam_name, max_hz=0)`.
  2. Daemon creates an `ImageStreamProducer`, allocates endpoint.
  3. Client calls `consumer.read()` — receives decoded `np.ndarray`.
  4. Client can also call `consumer.read_raw()` — returns `(frame_id, jpeg_bytes)`.
  5. Client iterates the consumer as a for-loop to process all frames.
- **Postconditions**: Consumer closed; no socket resources leaked.
- **Acceptance Criteria**:
  - [ ] `ImageStreamConsumer.read()` returns an `np.ndarray` (BGR) per frame.
  - [ ] `ImageStreamConsumer.read_raw()` returns `(frame_id, bytes)`.
  - [ ] `frame_id` in image stream matches `frame_id` in simultaneous tag stream.
  - [ ] Consumer is iterable via `for frame in consumer`.

## SUC-005: Live View Command Uses New Client API

- **Actor**: Developer running `aprilcam view`
- **Preconditions**: Daemon running; camera open
- **Main Flow**:
  1. `view_cli.py` calls `DaemonControl.get_image_stream(cam_name)` and
     `DaemonControl.get_tag_stream(cam_name)`.
  2. View renders frames from `ImageStreamConsumer`.
  3. Tag overlay is drawn from `TagFrame` objects from `TagStreamConsumer`.
  4. No raw `socket.socket()` calls remain in `view_cli.py`.
- **Postconditions**: View exits cleanly; both consumers closed.
- **Acceptance Criteria**:
  - [ ] `view_cli.py` contains no direct `socket.socket()` calls.
  - [ ] Live view renders correctly: frames visible, tag overlays present.
  - [ ] No import of `aprilcam.daemon.client` in `view_cli.py`.

## SUC-006: gRPC Reflection and Interface Discovery

- **Actor**: Developer debugging or integrating the daemon
- **Preconditions**: Daemon running with TCP transport active
- **Main Flow**:
  1. Developer runs `grpcurl -plaintext localhost:5280 list`.
  2. Server returns `aprilcam.AprilCam` in the service list.
  3. Developer runs `grpcurl -plaintext localhost:5280 describe aprilcam.AprilCam`.
  4. Full method and message schema is returned.
- **Postconditions**: None.
- **Acceptance Criteria**:
  - [ ] gRPC Server Reflection is enabled at startup.
  - [ ] `grpcurl list` returns the `AprilCam` service.
  - [ ] `grpcurl describe` returns method and message definitions.

## SUC-007: mDNS Advertisement

- **Actor**: Remote client on the same LAN
- **Preconditions**: Daemon running with `--tcp` enabled
- **Main Flow**:
  1. Daemon registers `_aprilcam._tcp.local.` via `zeroconf` at startup.
  2. Remote client browses mDNS and discovers the daemon's TCP port.
  3. Remote client connects to the advertised TCP endpoint.
  4. On daemon shutdown, the mDNS record is unregistered.
- **Postconditions**: mDNS record removed; no stale advertisement.
- **Acceptance Criteria**:
  - [ ] mDNS registration succeeds when `--tcp` is active.
  - [ ] No mDNS registration when `--no-tcp` is specified.
  - [ ] mDNS record is cleaned up on daemon exit.

## SUC-008: Homography Auto-Discovery at Playfield Start

- **Actor**: Python code using the `Playfield` API
- **Preconditions**: A calibration file exists for the camera; `Playfield` is constructed
- **Main Flow**:
  1. User code calls `playfield.start()`.
  2. `_auto_discover_homography()` is called after the camera is open; camera name and
     resolution are available.
  3. `discover_homography(device_name, width, height)` finds the calibration file.
  4. `self._homography` is set to the loaded matrix.
  5. Tag detection returns `tag.wx` and `tag.wy` with non-None world coordinates.
- **Postconditions**: Homography is loaded; world coordinates are valid.
- **Acceptance Criteria**:
  - [ ] `_auto_discover_homography()` calls `discover_homography` with correct arguments.
  - [ ] `self._homography` is a 3x3 numpy array (not None) when calibration exists.
  - [ ] `tag.wx` and `tag.wy` are non-None floats when homography is loaded.
  - [ ] Regression test passes: `test_playfield_homography_auto_discover`.
