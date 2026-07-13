---
status: approved
---

# Project Overview

## Project Name

AprilCam MCP Server

## Problem Statement

AI agents working with robotics playfields need to understand the
physical layout — tag positions, orientations, velocities, and visual
features — without performing their own vision processing. Currently,
the AprilCam project provides these capabilities through interactive
CLI tools designed for human use. There is no programmatic interface
that allows an AI agent to query camera feeds, detect fiducial markers,
perform homography, or run image-processing operations on live frames.

## Target Users

- **AI agents** (via MCP protocol) that need to perceive and reason
  about physical playfields with fiducial markers and moving objects.
- **Robotics developers** who want to use the CLI for tag generation,
  camera enumeration, and manual inspection alongside agent workflows.

## Key Constraints

- **Python package**, installable via `pipx install aprilcam`.
- **Single CLI entry point** (`aprilcam`) with subcommands:
  `mcp` (run MCP server), `taggen`, `cameras`, etc.
- **MCP transport**: stdio first, streamable HTTP later.
- **Local execution** required — camera hardware is attached to the
  host machine.
- **Existing codebase** — the core detection, homography, and tracking
  logic already exists and must be refactored, not rewritten.
- **Dependencies**: OpenCV (contrib), NumPy, mss, python-dotenv.
  Add an MCP server framework (e.g., `mcp` Python SDK or `fastmcp`).

## High-Level Requirements

### Camera Management

- **List cameras** — enumerate available cameras with names and indices.
- **Open/close camera** — open a camera by index or pattern, return a
  handle (camera_id). Support multiple simultaneous cameras.
- **Capture frame** — grab a single frame from a camera, return as
  base64 or temp file path (caller's choice).

### Playfield (Virtual Camera)

A playfield is a higher-level abstraction built on top of a camera.
It provides homography, deskew, and tag tracking.

- **Create playfield from camera** — specify a camera_id; the system
  detects ArUco corner markers and establishes the playfield polygon.
- **Deskew without calibration** — if ArUco corners form a rectangle,
  warp the image to a top-down view using pixel-only homography.
  No real-world measurements needed.
- **Calibrate with measurements** — provide real-world distances
  between corner markers (or a config file) to map pixels → cm/inches.
  This is a separate step the user can invoke after playfield creation.
- **Playfield as camera** — once created, a playfield_id can be used
  anywhere a camera_id is accepted. Queries against a playfield return
  the deskewed, homography-corrected view.

### Tag Detection & Tracking

- **Start/stop detection loop** — the agent controls a persistent
  detection loop on a camera or playfield. While running, the loop
  detects AprilTags and ArUco markers every frame, maintains a ring
  buffer of per-frame tag records (default 300 frames ≈ 10s at 30fps).
- **Query current tags** — return the latest tag detections: id,
  pixel position, orientation (yaw), world position (if calibrated),
  in-playfield flag.
- **Query tag history** — return the last N frames of tag records from
  the ring buffer. Enables the agent to compute or verify velocities,
  trajectories, and accelerations.
- **Velocity and motion** — each tag record includes computed velocity
  (px/s and world units/s if calibrated), speed, and heading.

### Image Processing Tools

Individual MCP tools that operate on a live frame from a camera or
playfield. Each returns structured data and/or an image.

- **detect_lines** — Hough line detection, returns line segments.
- **detect_circles** — Hough circle detection, returns centers/radii.
- **detect_contours** — contour detection with optional filtering,
  returns contour polygons.
- **detect_motion** — frame difference between current and previous
  frame, returns motion mask or changed regions.
- **detect_qr_codes** — QR code detection and decoding.
- **crop_region** — crop a rectangular region, return the sub-image.
- **apply_transform** — rotate, scale, threshold, edge-detect, or
  apply other OpenCV transformations to the current frame.
- **get_frame** — raw frame capture (no processing), as base64 or
  file path.

### Multi-Camera Compositing

- Support overlaying data from multiple cameras viewing the same
  playfield — e.g., a color camera for visual context and a global
  shutter B&W camera for reliable tag detection at speed.
- Tag positions extracted from the B&W camera; color information
  from the color camera.

### Tag Generation (CLI)

- Generate AprilTag 36h11 images (PNG) for a range of IDs.
- Generate 4x4 ArUco marker images for corner calibration.
- Available as `aprilcam taggen` and `aprilcam arucogen` subcommands.

### Image Return Format

- All tools that return images support two modes:
  - **base64** — inline in the MCP response.
  - **file** — written to a temp file, path returned.
- The agent specifies which format it wants per-request.

## Technology Stack

- **Language**: Python ≥ 3.9
- **Vision**: OpenCV (contrib) ≥ 4.8, NumPy ≥ 1.23
- **MCP**: Python MCP SDK (stdio transport; streamable HTTP later)
- **Screen capture**: mss ≥ 9.0
- **Config**: python-dotenv ≥ 1.0
- **Packaging**: pyproject.toml, pipx-installable
- **Testing**: pytest

## Sprint Roadmap

1. **Sprint 1 — Project Restructure & CLI Foundation**
   Reorganize the package for MCP server architecture. Set up the
   single CLI entry point with subcommands (`aprilcam mcp`, `taggen`,
   `arucogen`, `cameras`). Move the playfield simulator to a separate
   directory. Update pyproject.toml for pipx installation.

2. **Sprint 2 — Core MCP Server & Camera Tools**
   Implement the MCP server (stdio transport) with camera management
   tools: list_cameras, open_camera, close_camera, capture_frame.
   Establish the image return format (base64/file).

3. **Sprint 3 — Playfield & Homography**
   Implement playfield creation, ArUco corner detection, deskew,
   calibration workflow. Make playfield_id usable as camera_id.

4. **Sprint 4 — Tag Detection Loop & Ring Buffer**
   Implement start/stop detection loop, ring buffer storage, tag
   query tools (current state, history), velocity computation.

5. **Sprint 5 — Image Processing Tools**
   Implement individual image processing MCP tools: detect_lines,
   detect_circles, detect_contours, detect_motion, detect_qr_codes,
   crop_region, apply_transform.

6. **Sprint 6 — Multi-Camera Compositing**
   Support multiple cameras on the same playfield, overlay tag data
   from B&W camera onto color camera feed.

7. **Sprint 7 — Polish & Packaging**
   End-to-end testing, error handling, documentation, pipx install
   verification, performance tuning.

## Out of Scope

- **Web UI or dashboard** — no browser-based interface in this project.
- **Streamable HTTP transport** — deferred to a future phase after
  stdio is working.
- **Cloud deployment** — the server runs locally where cameras are
  attached.
- **Training or ML models** — uses classical CV (ArUco/AprilTag
  detection, Hough transforms), not learned models.
- **Playfield simulator** — the pygame simulator is preserved
  separately but is not part of the MCP server or CLI.
- **Non-live image processing** — image processing tools operate on
  live camera/playfield frames only, not arbitrary files from disk.
