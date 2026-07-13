---
sprint: "003"
status: draft
---

# Use Cases — Sprint 003: aprilcam view — Positional Arg + tkinter GUI

## SUC-001: Open a live view window by camera index

- **Actor**: Developer or robotics operator
- **Preconditions**: AprilCam daemon is running with a camera open (or will
  auto-start). `aprilcam view` is invoked with an integer positional argument.
- **Main Flow**:
  1. Operator runs `aprilcam view 2`.
  2. CLI resolves the camera by index via `open_camera` RPC.
  3. CLI connects to the daemon data socket and opens a tkinter window titled
     `"aprilcam view — <cam_name>"`.
  4. Window renders a live video canvas with tag overlays and a status bar.
- **Postconditions**: Window is open and updating at approximately 30 fps.
- **Acceptance Criteria**:
  - [ ] `aprilcam view 2` opens a tkinter window (not an OpenCV window).
  - [ ] Window title includes the resolved camera name.
  - [ ] Tag overlays and paths render correctly on the canvas.
  - [ ] Status bar shows FPS, tag count, calibration state, deskew mode.
  - [ ] `aprilcam view` (no arg) prints usage error and exits non-zero.

---

## SUC-002: Open a live view window by camera name pattern

- **Actor**: Developer or robotics operator
- **Preconditions**: Same as SUC-001, but invoked with a string name pattern.
- **Main Flow**:
  1. Operator runs `aprilcam view "Arducam"`.
  2. CLI calls `get_camera_info(cam_name=...)` to resolve the camera.
  3. Window opens identically to SUC-001.
- **Postconditions**: Window is open and updating.
- **Acceptance Criteria**:
  - [ ] `aprilcam view "Arducam"` resolves the camera and opens the window.
  - [ ] Unknown name prints an error and exits non-zero.

---

## SUC-003: Close the live view window cleanly

- **Actor**: Developer or robotics operator
- **Preconditions**: Window is open (SUC-001 or SUC-002 succeeded).
- **Main Flow**:
  1. Operator clicks window close (×), presses `q`, or presses `Escape`.
  2. tkinter main thread sets the stop event; reader thread exits.
  3. Data socket is closed; process exits 0.
- **Postconditions**: No orphan threads or hung process.
- **Acceptance Criteria**:
  - [ ] All three close paths (×, `q`, `Escape`) exit cleanly.
  - [ ] No error output on clean exit.
  - [ ] Process exits with return code 0.

---

## SUC-004: Status bar reflects live telemetry

- **Actor**: Developer or robotics operator (passive observation)
- **Preconditions**: Window is open, daemon is publishing frames with tag data.
- **Main Flow**:
  1. Operator observes the status bar while moving tags in and out of view.
  2. Tag count updates as tags appear and disappear.
  3. FPS reflects the daemon's reported frame rate.
  4. Calibrated and Deskew labels reflect current playfield state.
- **Postconditions**: Status labels are accurate within one poll cycle (33 ms).
- **Acceptance Criteria**:
  - [ ] FPS label updates each poll cycle.
  - [ ] Tag count changes when tags enter or leave the frame.
  - [ ] Calibrated and Deskew labels show correct boolean state.
