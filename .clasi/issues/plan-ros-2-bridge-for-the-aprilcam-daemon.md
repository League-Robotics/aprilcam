---
status: pending
---

# Plan: ROS 2 Bridge for the AprilCam Daemon

## Context

We want a ROS 2 node that exposes everything the AprilCam daemon does —
tag detections, camera images, camera control/queries, and live overlays —
so robots on a ROS 2 stack can consume AprilCam perception. The question
was whether to (a) run the daemon and connect a ROS node to it as a client,
or (b) import the daemon code into the ROS node process.

**Decision: a bridge node (option a).** The daemon runs as it does today;
the ROS node is a lightweight client that republishes into ROS. Rationale:

1. **The recent client/daemon dependency split pays off here** — the bridge
   needs only `pip install aprilcam` (grpcio/protobuf/pydantic/numpy) +
   rclpy, *not* OpenCV. It passes JPEG straight through as
   `sensor_msgs/CompressedImage` without decoding.
2. **Process isolation** — an OpenCV/camera crash can't take down the
   robot's ROS node, and vice versa.
3. **Python-version decoupling** — daemon runs 3.14; the ROS distro pins
   its own Python (Humble 3.10 / Jazzy 3.12 / Rolling 3.12+, all ≥3.10 so
   the client is compatible). A separate process sidesteps the coupling.

In-process embedding stays a possible fast-follow (cleanest after extracting
a plain-Python service façade shared by the gRPC servicer and a ROS node),
but is explicitly out of scope for v1.

### Confirmed scope (stakeholder)
ROS 2 (rclpy) · bridge architecture · expose **all** surfaces (tags, images,
control services, overlay subscribe) · tag detections in **both** a custom
rich message **and** native tf2 + `geometry_msgs/PoseArray`.

## What to reuse (do NOT reinvent the wire protocol)
- `src/aprilcam/client/control.py` — `DaemonControl` wraps every RPC
  (list/open/close cameras, get_camera_info, get_tags, where_is,
  get_image_stream, get_tag_stream, publish_overlay, shutdown).
- `src/aprilcam/client/stream.py` — `ImageStreamConsumer.read_image_frame()`
  (raw JPEG, **cv2-free**) and `TagStreamConsumer.read()` (TagFrame |
  OverlayFrame). Both are **blocking** length-prefixed socket reads → each
  runs in its own thread.
- `src/aprilcam/client/models.py` — `TagRecord`, `TagFrame`, `ImageFrame`,
  `CameraInfo`, `StreamEndpoint`: the source of truth for the message schema
  and conversions.
- `proto/aprilcam.proto` — authoritative field names/units and the overlay
  element format.

**cv2-free rule:** the bridge uses only `read_image_frame()` and the stream
JPEG path. It must **not** call `DaemonControl.capture_frame()` (that decodes
via cv2). The `capture_frame` service reads one frame off a transient image
stream instead.

## Package layout (monorepo, two ament packages)

New `ros2/` subtree in this repo (invisible to the existing setuptools build,
which globs `src/`). Custom messages need `rosidl`/ament_cmake, so split:

```
ros2/
  aprilcam_msgs/            # ament_cmake — .msg/.srv only
    package.xml  CMakeLists.txt
    msg/  AprilTagDetection.msg  AprilTagDetectionArray.msg
          OverlayElement.msg     OverlayCommand.msg
    srv/  ListCameras OpenCamera CloseCamera GetCameraInfo
          CaptureFrame WhereIs
  aprilcam_ros/            # ament_python — the node + pure logic
    package.xml setup.py setup.cfg resource/aprilcam_ros
    aprilcam_ros/
      bridge_node.py        # node: params, publishers, services, lifecycle
      daemon_session.py     # DaemonControl + connect/reconnect state machine
      stream_workers.py     # blocking tag/image consumer threads
      conversions.py        # PURE: cm→m, yaw datum, Pydantic→msg/tf mapping
      overlay_forwarder.py  # subscribe draw topic → publish_overlay
      discovery.py          # optional mDNS (zeroconf), guarded import
    launch/aprilcam_bridge.launch.py
    config/bridge.params.yaml
    test/test_conversions.py  test/test_msg_mapping.py
```

Dependencies: `aprilcam_ros/package.xml` depends on rclpy, std_msgs,
geometry_msgs, sensor_msgs, tf2_ros, `aprilcam_msgs`, python3-grpcio. The
`aprilcam` client has **no rosdep key** → primary install path is an explicit
`pip install aprilcam` into the ROS Python env (documented); ship an optional
local `rosdep/aprilcam.yaml` (`pip: aprilcam`) and `extras_require={"discovery":
["zeroconf>=0.131"]}`. Pin `aprilcam>=0.20260606.1` (introduced
`read_image_frame()` + the split client).

## Custom messages (`aprilcam_msgs`)
Rich message keeps **native daemon units** (cm, cm/s, radians) for fidelity;
geometry/tf surfaces emit meters. Optional Pydantic fields → paired
`bool has_*` + value (ROS msgs have no optionals).

- **AprilTagDetection.msg** — id; center_px x/y; corners_px[8]; yaw_rad;
  has_world + world_x_cm/world_y_cm + in_playfield; has_vel_px + vel_px
  x/y + speed_px; has_vel_world + vel_world x/y cmps + speed_world_cmps;
  has_heading + heading_rad; age_s.
- **AprilTagDetectionArray.msg** — std_msgs/Header; cam_name; frame_id; fps;
  calibrated; homography[9]; playfield_corners[8]; field_width_cm;
  field_height_cm; AprilTagDetection[] detections.
- **OverlayElement.msg** — type; params[] (world cm); uint8[3] color;
  thickness(-1=fill); text. **OverlayCommand.msg** — cam_name; ttl_s;
  OverlayElement[] elements.
- **srv/** — mirror DaemonControl. `CaptureFrame` returns
  `sensor_msgs/CompressedImage` (format="jpeg"). `WhereIs` returns
  `status`, `tokens[]`, and `matches_json` (JSON passthrough — the daemon's
  where_is result is heterogeneous nested dicts; modeling in .msg is
  high-cost/low-value).

## Node design (`aprilcam_ros`)
**One node per camera**, multi-instance via launch namespaces (`/cam0/…`).
Each camera = independent lifecycle + two blocking stream threads + own topics.

**Threading:** rclpy spins a `MultiThreadedExecutor` on the main thread. The
two stream consumers block on socket reads, so they run as dedicated
`threading.Thread`s, publishing directly (rclpy `Publisher.publish()` and the
tf broadcaster are thread-safe). `daemon_session.py` holds the `DaemonControl`,
the consumers, a state enum, and an exponential-backoff reconnect loop (on
`EOFError`/socket error → reconnect, re-attach/open camera, re-request streams,
restart workers). Services + overlay subscriber run on the executor with a
`ReentrantCallbackGroup`; control-plane gRPC calls serialized behind one lock.
Shutdown: stop flag → `consumer.close()` (unblocks recv) → join → close
DaemonControl → optional `close_camera`.

Key params (`config/bridge.params.yaml`): daemon_unix_socket / daemon_host /
daemon_port / use_mdns_discovery; camera_index / camera_name / open_on_start /
close_on_shutdown; image_max_hz / tag_max_hz; publish_images/tags/tf/pose_array;
enable_overlay_subscriber; world_frame_id="playfield", image_frame_id,
tag_frame_prefix="tag_", cm_to_m=0.01, yaw_offset_rad, yaw_sign,
publish_static_world_tf + map_frame_id; reconnect backoff; use_daemon_wall_clock.

### Topics & services (namespace `<ns>`)
| Surface | Name | Type | QoS |
|---|---|---|---|
| Rich tags | `<ns>/detections` | `aprilcam_msgs/AprilTagDetectionArray` | reliable, depth 5 |
| Poses | `<ns>/tag_poses` | `geometry_msgs/PoseArray` | reliable, depth 5 |
| TF / static | `/tf`, `/tf_static` | via Transform(Static)Broadcaster | tf defaults |
| Images | `<ns>/image/compressed` | `sensor_msgs/CompressedImage` | SensorDataQoS (best-effort) |
| Overlay in | `<ns>/overlay/draw` | `aprilcam_msgs/OverlayCommand` | reliable, depth 10 |

Services under `<ns>/`: `list_cameras`, `open_camera`, `close_camera`,
`get_camera_info`, `capture_frame` (transient-stream read → CompressedImage,
no cv2), `where_is`. `<ns>/image/compressed` follows the `image_transport`
convention so RViz/republish work out of the box.

## Coordinate conversions (`conversions.py`, all pure, all unit-tested)
Daemon world: origin at AprilTag id 1, **+x east, +y north, cm**, right-handed
(+z up) — already a valid REP-103 world frame, so **position = identity + cm→m
scale** (lossless). The error-prone part is the **yaw datum**: daemon yaw is
from **+Y**, ROS yaw from **+X**:

```
ros_yaw = yaw_offset_rad + yaw_sign * daemon_yaw   # default offset=pi/2, sign=+1  →  ros_yaw = pi/2 - daemon_yaw
```

Named functions: `cm_to_m`, `world_cm_to_point`, `daemon_yaw_to_ros_yaw`,
`yaw_to_quaternion` (about +z), `tagrecord_to_pose` (→ None when uncalibrated),
`tagframe_to_detection_array`, `tagframe_to_transforms`, `tagrecord_to_detection_msg`,
`stamp_from_tagframe`. tf child frames `tag_<id>` parented to `world_frame_id`;
only calibrated tags (world_xy present) get tf + PoseArray entries, but **all**
tags appear in the rich array. Static `map`→`playfield` identity tf provided so
users re-parent without code changes. `ts_mono_ns` is host-monotonic → carried
informationally only; header stamp from `ts_wall_ms` (or node clock if
`use_daemon_wall_clock=false`, e.g. under `use_sim_time`).

## Install & run
```bash
# camera host (heavy):    pip install "aprilcam[daemon]"; aprilcam daemon
# ROS host (light):       pip install aprilcam
# build:  symlink ros2/aprilcam_msgs + ros2/aprilcam_ros into a ws/src,
#         rosdep install …; colcon build --packages-select aprilcam_msgs aprilcam_ros
# run:    ros2 launch aprilcam_ros aprilcam_bridge.launch.py camera_index:=0
```
**v1 co-locates the bridge with the daemon over Unix sockets.** Cross-host
TCP streaming is a documented follow-up: `client/stream.py` currently dials
`localhost` for TCP stream endpoints, so cross-host needs a small change to
honor the daemon host in `StreamEndpoint`/consumers. (gRPC control already
honors host; only the raw stream sockets are localhost-bound.)

## Verification
- **Pure unit tests (plain pytest, no ROS):** `test_conversions.py` —
  yaw datum (yaw=0→pi/2; yaw=pi/2→0), quaternion round-trip, 100cm→1.0m,
  uncalibrated→None. `test_msg_mapping.py` — build a `TagFrame` Pydantic
  object, assert field-by-field mapping, has_* flags, homography[9],
  empty arrays when uncalibrated, header-stamp selection (needs
  `aprilcam_msgs` built, no daemon/spin).
- **Mock-daemon integration (no hardware):** `FakeDaemonControl` + fake
  consumers replay scripted frames; spin the node, assert published msgs via
  a test subscriber; simulate `EOFError` → assert reconnect.
- **Smoke:** `aprilcam daemon` + launch node; `ros2 topic echo /cam0/detections`,
  `ros2 topic hz /cam0/image/compressed`, `ros2 service call /cam0/list_cameras …`,
  `ros2 topic pub --once /cam0/overlay/draw …` (verify overlay appears in the
  AprilCam live view). RViz: Fixed Frame `playfield`, add TF + PoseArray +
  compressed Image.

## Phased delivery
0. Scaffold both packages; empty colcon build green.
1. Author all `.msg`/`.srv`; build `aprilcam_msgs`; generated types import.
2. `conversions.py` + pure tests green (reuses `aprilcam.client.models`).
3. `daemon_session.py` + `stream_workers.py`: connect, open camera, publish
   detections/poses/tf/images, reconnect/backoff.
4. Six services + `overlay_forwarder.py` (cv2-free capture).
5. Launch file, params yaml, `ros2/README.md` (install/run/RViz).
6. Mock-daemon + RViz smoke; document the cross-host caveat.

## Key risks
1. **Cross-host TCP streaming** (highest) — localhost-bound stream sockets;
   v1 co-locates over Unix sockets, follow-up threads host through.
2. **rclpy thread-safe publish** — relied upon from worker threads; keep
   publish sites centralized so a `queue.Queue`+timer fallback is a one-spot
   change if a future rmw isn't thread-safe.
3. **Time sync** — daemon wall clock vs ROS `/clock`; `use_daemon_wall_clock`
   param, default documented (set false under use_sim_time).
4. **Two-package rosidl/ament build** — gate generated-type tests behind a
   built workspace.
5. **Double-hop latency** — JPEG passed through un-decoded; tens of ms added,
   fine for 5–30 Hz; `max_hz` caps load.
