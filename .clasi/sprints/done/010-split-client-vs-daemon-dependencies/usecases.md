---
status: draft
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 010 Use Cases

## SUC-001: Developer Installs Lightweight Client
Parent: UC-Client-Install

- **Actor**: Developer who wants to talk to a running AprilCam daemon from a remote
  or resource-constrained machine.
- **Preconditions**: Python ≥ 3.10 available; no OpenCV, no camera hardware required.
- **Main Flow**:
  1. Developer runs `pip install aprilcam` (base install, no extras).
  2. Package installs successfully with only lightweight dependencies:
     `grpcio`, `protobuf`, `pydantic`, `numpy`, `python-dotenv`, `rich`.
  3. Developer imports `from aprilcam.client import DaemonControl` — succeeds.
  4. Developer runs `aprilcam --help`, `aprilcam --version`, `aprilcam init` — all work.
  5. `pip list` shows no `opencv-contrib-python`, `mss`, `mcp`, `jupyter`, `anthropic`.
- **Postconditions**: Client is installed; developer can connect to a remote daemon over
  gRPC without requiring OpenCV or the heavy server stack.
- **Acceptance Criteria**:
  - [ ] `pip install aprilcam` (base) succeeds in a fresh venv.
  - [ ] `from aprilcam.client import DaemonControl` succeeds with no OpenCV present.
  - [ ] `python -c "import cv2"` **fails** after a base install (confirms no opencv pulled in).
  - [ ] `aprilcam --help`, `aprilcam --version` work on the base install.
  - [ ] `pip list` shows none of: opencv, mss, mcp, jupyter, anthropic, fpdf2, pillow.

---

## SUC-002: Operator Installs Full Daemon
Parent: UC-Daemon-Install

- **Actor**: Robotics operator who needs to run the AprilCam daemon and MCP server
  on a machine with attached cameras.
- **Preconditions**: Python ≥ 3.10, camera hardware attached.
- **Main Flow**:
  1. Operator runs `pip install 'aprilcam[daemon]'`.
  2. Package installs with the full heavyweight stack: OpenCV, mss, mcp, websockets,
     fpdf2, cv2-enumerate-cameras, msgpack, pillow, grpcio-reflection, zeroconf.
  3. Operator starts the daemon: `aprilcam daemon ...` — succeeds.
  4. Operator starts the MCP server: `aprilcam mcp` — succeeds.
  5. A connected client calls `get_version()` over MCP — returns a valid response.
  6. A connected client calls `capture_frame` — returns a decoded frame successfully.
- **Postconditions**: Full daemon and MCP server are running; camera operations
  and image processing are available.
- **Acceptance Criteria**:
  - [ ] `pip install 'aprilcam[daemon]'` succeeds in a fresh venv.
  - [ ] Daemon starts and accepts gRPC connections.
  - [ ] `aprilcam mcp` starts the MCP server; `get_version()` returns a valid response.
  - [ ] `capture_frame` round-trip succeeds (frame decodes without error).

---

## SUC-003: Client Decodes Frame Without Imaging Extra — Guided Error
Parent: UC-Client-Install

- **Actor**: Developer who has the base `aprilcam` install (no `[imaging]` extra)
  and calls a frame-decode method.
- **Preconditions**: Base install only; `opencv-contrib-python` is NOT installed.
- **Main Flow**:
  1. Developer calls `DaemonControl.capture_frame(cam_name)` or uses
     `ImageStreamConsumer.read()`.
  2. The method detects that `cv2` is unavailable.
  3. Instead of a bare `ImportError`, the method raises a `RuntimeError` with the
     message: *"Decoding camera frames requires OpenCV. Install it with
     `pip install 'aprilcam[imaging]'` (or `aprilcam[daemon]`)."*
- **Postconditions**: Developer receives a clear, actionable error message directing
  them to the correct install command.
- **Acceptance Criteria**:
  - [ ] Calling `capture_frame` without OpenCV raises `RuntimeError` (not `ImportError`).
  - [ ] The error message explicitly mentions `aprilcam[imaging]`.
  - [ ] No top-level `import cv2` occurs in `client/control.py` or `client/stream.py`
        (verified by inspection and by importing the module without opencv present).

---

## SUC-004: Developer Tools Isolated to Dev Extra
Parent: UC-Developer-Workflow

- **Actor**: Developer building or extending AprilCam.
- **Preconditions**: Wants jupyter, ipython, anthropic, and grpcio-tools available
  for development but not in production installs.
- **Main Flow**:
  1. Developer runs `pip install 'aprilcam[dev]'`.
  2. Dev tools (ipykernel, ipython, jupyter, anthropic, grpcio-tools) install.
  3. Standard `pip install aprilcam` installs have none of these packages.
- **Postconditions**: Dev tooling is available in dev environments; not imposed on
  production or client-only installs.
- **Acceptance Criteria**:
  - [ ] `[dev]` extra lists ipykernel, ipython, jupyter, anthropic, grpcio-tools.
  - [ ] Base install does not include any of these packages.

---

## SUC-005: Test Suite Passes in Both Client-Only and Daemon Configurations
Parent: UC-Testing

- **Actor**: CI system or developer running `pytest`.
- **Preconditions**: Either (a) base install only, or (b) full `[daemon]` install.
- **Main Flow** (client-only):
  1. `pytest` runs against the base install.
  2. Tests that require `cv2` or daemon modules are skipped or marked.
  3. `tests/test_client_models.py` and other pure-client tests pass.
- **Main Flow** (daemon install):
  1. `pytest` runs against the `[daemon]` install.
  2. All tests, including `tests/test_daemon_control.py`, pass.
- **Postconditions**: No test failures due to missing optional dependencies in
  either configuration.
- **Acceptance Criteria**:
  - [ ] `pytest` exits 0 in a base-install venv (daemon tests skipped).
  - [ ] `pytest` exits 0 in a `[daemon]`-install venv (all tests run).
  - [ ] Tests needing cv2 carry a `pytest.mark.needs_cv2` (or equivalent) skip marker.
