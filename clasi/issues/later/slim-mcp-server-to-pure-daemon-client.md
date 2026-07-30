---
status: pending
---

# Slim the MCP server to a pure daemon client

Per [specification.md](../../docs/design/specification.md) §4.7: the MCP
server must contain no OpenCV and no vision code — it is a thin daemon
client exposing perception + configuration tools. Today it has 44 tools,
several of which import cv2 locally (violating the Sprint-015 vision
boundary that the project already claims to enforce).

## Remove

- The frame-processing registry and its tools: `create_frame`,
  `create_frame_from_image`, `process_frame`, `get_frame_image`,
  `save_frame`, `release_frame`, `list_frames`; `server/frame.py`.
- Local-cv2 playfield tools: `create_playfield_from_image`,
  `deskew_image`, and the local calibration code paths (replaced by the
  daemon Calibrate RPC — [[move-calibration-execution-into-daemon]]).
- Path and overlay tools (see
  [[replace-paths-service-with-client-drawing-library]]).
- `_require_cv2` and every cv2 import.

## Keep / add

- Perception tools: cameras (list/open/close/capture), `get_frame`
  (raw and deskewed), detection (get_tags, history, objects), `where`
  (from the playfield definition), mobile-tag registration, calibration
  trigger.
- Configuration tools (whole-document): get/set playfield, get/set/reset
  camera config, list playfields/cameras with links
  ([[daemon-playfield-config-service]],
  [[daemon-camera-config-service-reset]]).
- Pixel↔world conversion from daemon-published homography/metadata.
- The vision-boundary contract for consumers: pixel work = `get_frame`
  + process the JPEG yourself.

## Acceptance sketch

`aprilcam mcp` starts and every remaining tool works on a base install
with no OpenCV present; `grep -r "import cv2\|_require_cv2"` under the
MCP package returns nothing.
