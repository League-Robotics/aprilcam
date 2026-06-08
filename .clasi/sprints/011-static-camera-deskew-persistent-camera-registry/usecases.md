---
status: draft
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 011 Use Cases

## SUC-001: Deskew a fixed camera from its saved homography
Parent: Playfield / Deskew without calibration (overview "Playfield")

- **Actor**: Robotics developer / AI agent running the daemon on a fixed-mount camera.
- **Preconditions**: The camera was calibrated previously; `calibration.json`
  holds a valid homography and playfield width/height. The camera has not moved.
- **Main Flow**:
  1. The pipeline loads the saved calibration for the camera.
  2. The system computes the four playfield-corner pixel positions by mapping
     world corners `(0,0),(W,0),(W,H),(0,H)` through `H⁻¹`.
  3. The deskew source polygon is seeded from those pixels (no live ArUco
     detection required).
  4. Frames warp to a top-down view using the seeded polygon.
- **Postconditions**: A flat top-down view is produced even when no ArUco
  corner is detected in any live frame.
- **Acceptance Criteria**:
  - [ ] With a saved calibration and a physically covered corner marker,
    deskew still produces a warped view.
  - [ ] The extreme-angle global-shutter camera produces a flat top-down view.
  - [ ] Deskew falls back to the existing live-corner polygon path when no
    saved homography exists.

## SUC-002: Tolerate intermittent static markers via fill-in
Parent: Tag Detection & Tracking (overview)

- **Actor**: Daemon detection pipeline on a fixed camera.
- **Preconditions**: Static-camera mode is active; static markers (ArUco
  corners + AprilTag 1) have known calibration-time pixel positions.
- **Main Flow**:
  1. Each frame, the detector reports whichever markers it finds.
  2. For a static marker not found this frame, the system holds its stored
     pixel position instead of dropping it.
  3. Dynamic AprilTags (ID ≠ 1) are taken only from this frame's detections.
- **Postconditions**: Polygon and static reference set remain stable under
  flicker; dynamic tags are never frozen at stale positions.
- **Acceptance Criteria**:
  - [ ] A static marker missing from a frame retains its stored position.
  - [ ] Dynamic (robot-mounted) tags are tracked frame-by-frame and never held.
  - [ ] The static-marker set defaults to `aruco_corners + apriltag:1` and is
    configurable.

## SUC-003: Detect camera movement and invalidate stale geometry
Parent: Calibrate with measurements (overview "Playfield")

- **Actor**: Daemon detection pipeline on a fixed camera.
- **Preconditions**: Static-camera mode is active with stored static-marker
  positions.
- **Main Flow**:
  1. When static markers are detected live, the system compares their live
     pixel positions to the stored positions.
  2. If disagreement exceeds a threshold, the static assumption is dropped.
  3. A warning is surfaced prompting recalibration.
- **Postconditions**: The system does not silently deskew with a stale
  transform after the camera is bumped.
- **Acceptance Criteria**:
  - [ ] Moving the camera after calibration is detected and surfaced rather
    than silently deskewed.
  - [ ] AprilTag 1 functions as a movement sentinel even when edge corners are
    occluded.

## SUC-004: Optional distortion correction before warp
Parent: Playfield / Homography (overview)

- **Actor**: Daemon detection pipeline.
- **Preconditions**: Saved calibration includes `camera_matrix` and
  `dist_coeffs`.
- **Main Flow**:
  1. When enabled, the frame is undistorted with the saved intrinsics.
  2. The perspective warp is applied to the undistorted frame.
- **Postconditions**: Residual barrel curvature is visibly flattened.
- **Acceptance Criteria**:
  - [ ] When intrinsics exist, optional undistortion visibly flattens residual
    barrel curvature.
  - [ ] When intrinsics are absent, the path is a no-op and deskew still works.

## SUC-005: Identify a camera by stable hardware identity
Parent: Camera Management / List cameras (overview)

- **Actor**: Daemon / `aprilcam cameras` CLI.
- **Preconditions**: One or more cameras are attached.
- **Main Flow**:
  1. On enumeration, the system captures a stable hardware id (serial /
     VID:PID+location / AVFoundation uniqueID) for each camera.
  2. When no serial exists, it falls back to the USB location path and records
     the limitation.
- **Postconditions**: Two identical-model cameras are distinguishable by id.
- **Acceptance Criteria**:
  - [ ] Plugging two of the same model yields two distinct registry entries
    with distinct ids.
  - [ ] The fallback id (USB location path) is used and documented when no
    serial is available.

## SUC-006: Persist a camera registry with stable enumeration numbers
Parent: Camera Management / Open camera (overview)

- **Actor**: Daemon.
- **Preconditions**: Registry module is available; a camera is seen.
- **Main Flow**:
  1. On first sight, the camera is assigned the next monotonic enumeration
     number and a registry record is written.
  2. On reconnect, the camera is looked up by unique id and its existing
     number and data dir are reused.
  3. `OpenCamera` resolves the camera through the registry.
- **Postconditions**: Re-plugging a known camera requires no daemon restart;
  enumeration numbers are stable.
- **Acceptance Criteria**:
  - [ ] Unplug + replug of a known camera reuses its enumeration number and
    per-camera data dir with no daemon restart and no manual reindex.
  - [ ] A genuinely new camera receives a new monotonic number.
  - [ ] Existing `data/aprilcam/cameras/<slug>/` data is preserved or migrated,
    not orphaned.

## SUC-007: List connected and previously-seen cameras
Parent: Camera Management / List cameras (overview)

- **Actor**: Robotics developer running `aprilcam cameras`.
- **Preconditions**: The registry contains both connected and disconnected
  cameras.
- **Main Flow**:
  1. The CLI reads the registry plus the live device list.
  2. It prints every registered camera with its enumeration number.
  3. Connected cameras show their current OS index; disconnected cameras are
     shown grayed-out as offline.
- **Postconditions**: History is visible; identity and number persist across
  disconnects.
- **Acceptance Criteria**:
  - [ ] `aprilcam cameras` lists connected and previously-seen-disconnected
    cameras, the latter grayed out, all retaining enumeration numbers.
  - [ ] Connected cameras display their current OS index.
