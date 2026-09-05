---
source_file: design.md
source_hash: 35ba372c4afbf192b2e4a4f3385d4b594097fb16d524df081c8efd2d9cbf25e4
---
# Diff: design.md

Comparison of the sprint overlay copy of `design.md` against its pristine (seed-commit) canonical version.

```diff
--- design.md (pristine)
+++ design.md (current)
@@ -117,7 +117,7 @@
 | Subsystem | Doc | Role | cv2? |
 |---|---|---|---|
 | `daemon/` | [DESIGN.md](../../src/aprilcam/daemon/DESIGN.md) | The `aprilcamd` process: camera ownership, per-camera detection pipeline, gRPC control plane + stream transport, config service, mobile-tag registry, annotation store | yes |
-| `camera/` | [DESIGN.md](../../src/aprilcam/camera/DESIGN.md) | Camera identity resolution across re-plugs, the persistent registry, pluggable capture backends (OpenCV/v4l2loopback/uvc-util) | yes |
+| `camera/` | [DESIGN.md](../../src/aprilcam/camera/DESIGN.md) | Camera identity resolution across re-plugs, the persistent registry, pluggable capture backends (OpenCV/v4l2loopback/uvc-util/per-camera worker process, sprint 038) | yes |
 | `vision/` | [DESIGN.md](../../src/aprilcam/vision/DESIGN.md) | Stateless frame vision: preprocessing pipeline, AprilTag/ArUco raw detection, world-position kinematics | yes |
 | `calibration/` | [DESIGN.md](../../src/aprilcam/calibration/DESIGN.md) | Pixel↔world calibration engine (homography, lens distortion, intrinsics), deskew geometry, mount/parallax correction, flat-field gain-map capture (sprint 037) | yes |
 | `playfield/` | [DESIGN.md](../../src/aprilcam/playfield/DESIGN.md) | The surveyed playfield as a definition document plus feature queries ("where is the blue dot") | no |
@@ -140,6 +140,7 @@
 | Runtime piece | Kind | Notes |
 |---|---|---|
 | `aprilcamd` | long-running daemon | flock singleton per machine; unix socket + TCP 5280; mDNS `_aprilcam._tcp.local.` |
+| capture worker (per opened camera) | child process, sprint 038 | spawned by `aprilcamd` via `camera/backends/worker_backend.py` (`python -m aprilcam.camera.capture_worker`); owns the real `cv2.VideoCapture`, publishes frames over a shared-memory ring to its parent. Config/env-gated — not present at all when a camera uses `OpenCVBackend` (the unchanged default). Killed and respawned by the parent on self-heal; never addressed directly by any client. |
 | `aprilcam mcp` | stdio process per agent session | pure daemon client |
 | `aprilcam view` | GUI process | basic viewing only |
 | CLI commands | short-lived | thin daemon clients |
@@ -322,3 +323,16 @@
    enforced `_CV2_FREE_PACKAGES` set, so a future change reintroducing a
    `cv2` import into `cli/` would not be caught by that gate — this
    remains a discipline/code-review boundary, not a test-gated one.
+7. **Resolved, sprint 038: the process-local AVFoundation capture-session
+   wedge** (`clasi/issues/camera-capture-session-wedge-is-silent-and-needs-a-daemon-restart.md`)
+   — root-caused to per-process `cv2.VideoCapture`/AVFoundation state
+   that `release()` never fully tears down, confirmed by a fresh process
+   reading the exact same wedged index cleanly. Fixed structurally via a
+   per-camera capture worker process (`camera/DESIGN.md` §4) rather than
+   any in-process reopen strategy, which the finding showed cannot work.
+   Alongside it, `usable` now requires a real frame read
+   (`camera/DESIGN.md` §3) and `aprilcam camera view` fails within a
+   bounded timeout instead of hanging forever (`view/DESIGN.md` §3,
+   `cli/DESIGN.md` §3). Whether the wedge correlates with UVC control
+   writes during an active stream remains genuinely open (carried
+   forward in `camera/DESIGN.md` §6) but no longer blocks the fix.
```
