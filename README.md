# AprilCam MCP Server

MCP server and CLI tools for AI agents to interact with cameras,
AprilTag/ArUco fiducial marker detection, playfield homography, and
image processing. Designed for robotics playfields where agents need
to perceive tag positions, orientations, velocities, and visual
features without performing their own vision processing.

## Installation

AprilCam is split into install tiers so you only pull in what you need.

| Tier | Command | What you get |
|------|---------|--------------|
| **Client** (default) | `pip install aprilcam` | Lightweight gRPC client — connect to a running daemon. No OpenCV required. |
| **Imaging** | `pip install 'aprilcam[imaging]'` | Client + frame-decode support (adds OpenCV). |
| **Daemon / MCP server** | `pip install 'aprilcam[daemon]'` | Full server stack — run the daemon and MCP server locally. |
| **Dev** | `pip install 'aprilcam[dev]'` | Development tools (jupyter, ipython, anthropic SDK). |

To run the daemon or MCP server, install the `daemon` extra:

```
pip install 'aprilcam[daemon]'
```

With pipx (for isolated global install of the full server):

```
pipx install 'aprilcam[daemon]'
```

With uv:

```
uv pip install 'aprilcam[daemon]'
```

## Requirements

- Python >= 3.9
- Base client: grpcio, protobuf
- `[imaging]` extra adds: opencv-contrib-python >= 4.8, numpy >= 1.23
- `[daemon]` extra adds: mcp >= 1.0, numpy >= 1.23, opencv-contrib-python >= 4.8, and all daemon dependencies

## MCP Server

Start the MCP server (stdio transport):

```
aprilcam mcp
```

### Claude Code configuration

Add to your `.mcp.json`:

```json
{
  "mcpServers": {
    "aprilcam": {
      "command": "aprilcam",
      "args": ["mcp"]
    }
  }
}
```

## MCP Tools Reference

### Camera Management

- **list_cameras** -- Enumerate available cameras with names and indices.
- **open_camera** -- Open a camera by index or name pattern; returns a camera_id handle.
- **capture_frame** -- Grab a single frame from a camera (base64 or file path).
- **close_camera** -- Release a camera handle.

### Playfield and Homography

- **create_playfield** -- Create a playfield from a camera using ArUco corner markers; establishes the playfield polygon and homography.
- **create_playfield_from_image** -- Create a playfield from a static image file.
- **calibrate_playfield** -- Provide real-world measurements to map pixels to physical units.
- **deskew_image** -- Warp a playfield view to a top-down rectangular image.
- **get_playfield_info** -- Query playfield state: corners, calibration, polygon.

### Tag Detection and Tracking

- **start_detection** -- Start a persistent detection loop on a camera or playfield; maintains a ring buffer of per-frame tag records.
- **stop_detection** -- Stop the detection loop.
- **get_tags** -- Return the latest tag detections: id, position, orientation, velocity.
- **get_tag_history** -- Return the last N frames of tag records from the ring buffer.

### Image Processing

- **get_frame** -- Raw frame capture (no processing), as base64 or file path.
- **crop_region** -- Crop a rectangular region and return the sub-image.
- **detect_lines** -- Hough line detection; returns line segments.
- **detect_circles** -- Hough circle detection; returns centers and radii.
- **detect_contours** -- Contour detection with optional filtering.
- **detect_motion** -- Frame difference motion detection; returns changed regions.
- **detect_qr_codes** -- QR code detection and decoding.
- **apply_transform** -- Rotate, scale, threshold, edge-detect, or apply other OpenCV transformations.

### Multi-Camera Compositing

- **create_composite** -- Combine multiple cameras viewing the same playfield.
- **get_composite_frame** -- Capture a frame from a composite source.
- **get_composite_tags** -- Get tag detections merged across composite cameras.

## CLI Commands

```
aprilcam mcp        # Start the MCP server
aprilcam taggen     # Generate AprilTag 36h11 images (PNG)
aprilcam arucogen   # Generate 4x4 ArUco marker images
aprilcam cameras    # List available cameras
```

## License

MIT
