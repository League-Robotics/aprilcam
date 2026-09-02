---
source_file: design.md
source_hash: c97af04437fd3ac59653eaa35768cc9f3881ad01a3e82376b49196284f26f114
---
# Diff: design.md

Comparison of the sprint overlay copy of `design.md` against its pristine (seed-commit) canonical version.

```diff
--- design.md (pristine)
+++ design.md (current)
@@ -119,7 +119,7 @@
 | `daemon/` | [DESIGN.md](../../src/aprilcam/daemon/DESIGN.md) | The `aprilcamd` process: camera ownership, per-camera detection pipeline, gRPC control plane + stream transport, config service, mobile-tag registry, annotation store | yes |
 | `camera/` | [DESIGN.md](../../src/aprilcam/camera/DESIGN.md) | Camera identity resolution across re-plugs, the persistent registry, pluggable capture backends (OpenCV/v4l2loopback/uvc-util) | yes |
 | `vision/` | [DESIGN.md](../../src/aprilcam/vision/DESIGN.md) | Stateless frame vision: preprocessing pipeline, AprilTag/ArUco raw detection, world-position kinematics | yes |
-| `calibration/` | [DESIGN.md](../../src/aprilcam/calibration/DESIGN.md) | Pixel↔world calibration engine (homography, lens distortion, intrinsics), deskew geometry, mount/parallax correction | yes |
+| `calibration/` | [DESIGN.md](../../src/aprilcam/calibration/DESIGN.md) | Pixel↔world calibration engine (homography, lens distortion, intrinsics), deskew geometry, mount/parallax correction, flat-field gain-map capture (sprint 037) | yes |
 | `playfield/` | [DESIGN.md](../../src/aprilcam/playfield/DESIGN.md) | The surveyed playfield as a definition document plus feature queries ("where is the blue dot") | no |
 | `config/` | [DESIGN.md](../../src/aprilcam/config/DESIGN.md) | `config_dir`/`state_dir`/`socket_dir` resolution, and the config-document schema/loader (camera configs, playfield definitions) | no |
 | `client/` | [DESIGN.md](../../src/aprilcam/client/DESIGN.md) | The one sanctioned way to talk to a daemon: gRPC wrapper, discovery, stream consumers, `from_proto` model boundary, annotation renderer | no *(enforced)* |
@@ -154,9 +154,13 @@
 
 - **Control plane:** gRPC (proto in `proto/aprilcam_v2.proto`, regenerated
   via `scripts/compile_proto.py`). Camera lifecycle, one-shot queries,
-  stream endpoint allocation, calibration trigger, config service
-  (`SetPlayfield`/`GetPlayfield`, `SetCameraConfig`/`GetCameraConfig`/
-  `ResetCameraConfig`), mobile-tag registry, annotation store
+  stream endpoint allocation, calibration trigger, flat-field gain-map
+  capture (`CaptureFlatField`/`GetFlatField`/`ClearFlatField` — sprint
+  037; mirrors `Calibrate`/`GetCalibration`'s shape exactly, including
+  `force`/`dry_run` request flags and a data-not-exception quality-gate
+  result), config service (`SetPlayfield`/`GetPlayfield`,
+  `SetCameraConfig`/`GetCameraConfig`/`ResetCameraConfig`), mobile-tag
+  registry, annotation store
   (`PutAnnotation`/`RemoveAnnotation`/`ReplaceLayer`/
   `ClearAnnotations`/`GetAnnotations`, playfield-addressed). `GetStatus`
   additionally carries a `protocol_version` field (sprint 031) so a v2
@@ -227,6 +231,13 @@
 state_dir/                         # regenerable / runtime state
   cameras/registry.json            # identity -> enum + camera dir
   cameras/<cam>/calibration.json   # homography, static markers, provenance
+  cameras/<cam>/flatfield.npy      # per-pixel/channel gain map (apply side
+                                   #   shipped out-of-process, commit 7f4c0ac;
+                                   #   capture side sprint 037)
+  cameras/<cam>/flatfield.json     # metadata sidecar: captured_at, shape,
+                                   #   gain_min/max, stale (sprint 037,
+                                   #   mirrors calibration.json's own
+                                   #   provenance+stale fields)
   cameras/<cam>/info.json          # advisory: calibration_stale, frame_dark
   mounts/registry.json             # TagId -> MountParameters (sprint 036;
                                    #  previously in-memory only, register at
```
