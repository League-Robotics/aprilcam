---
status: pending
---

# Move the last MCP-side vision (calibration, compositing, process_frame) into the daemon

Follow-up from sprint 015. That sprint made the MCP **perception path** (tags,
objects, where, frames, tag stream) and the `web`/`view` clients fully
opencv-free, and moved `opencv-contrib` to the `daemon` extra. A few MCP tools
still run OpenCV at call time and are now **gated** (they raise an actionable
"install `aprilcam[daemon]`" message when opencv is absent) rather than moved:

- `calibrate_playfield` — ArUco corner detection + homography compute on captured
  frames (`server/mcp_server.py`).
- multi-camera **compositing** — `create_composite` / `get_composite_frame`
  (`camera/composite.py` cross-camera homography + tag mapping).
- `process_frame` / `_detect_tags_on_frame`, `deskew_image`,
  `create_playfield_from_image` — pixel ops on frames/disk images.

To finish "the daemon is the sole vision authority," move this compute to the
daemon and expose it via RPCs so the MCP stays a pure client:

- **`CalibratePlayfield` RPC**: daemon detects ArUco corners on its own frames,
  computes + saves the homography (extends the existing `SetCalibration`/
  `ReloadCalibration` path); MCP `calibrate_playfield` becomes a thin call.
- **Compositing**: decide whether multi-camera compositing belongs in the daemon
  (it owns all cameras) and expose `GetCompositeFrame`/`GetCompositeTags` RPCs, or
  drop it if unused.
- **`process_frame` / `deskew_image` / `create_playfield_from_image`**: per the
  sprint-015 boundary ("consumers fetch a frame and run their own CV"), these are
  candidates for **removal** from the MCP (like the image-processing tools were),
  unless a daemon-side equivalent is wanted.

Outcome: MCP server has **zero** call-time OpenCV; the `daemon` extra is needed
only to run the daemon itself. Builds on [[project_daemon_sole_camera_owner]] and
the sprint-015 thin-client work (v0.20260619.10).
