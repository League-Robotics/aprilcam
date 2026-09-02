---
source_file: mcp-DESIGN.md
source_hash: c88b561948db738c966547cbbc63779b667e19caeded07cba9f090efe446eb26
---
# Diff: mcp-DESIGN.md

Comparison of the sprint overlay copy of `mcp-DESIGN.md` against its pristine (seed-commit) canonical version.

```diff
--- mcp-DESIGN.md (pristine)
+++ mcp-DESIGN.md (current)
@@ -31,7 +31,7 @@
 | `errors.py` | `handle_tool_errors` — maps `aprilcam.errors.AprilCamError` and any other exception to a clean MCP `ToolError` |
 | `images.py` | `create_temp_dir` / `image_response` — shapes already-encoded image bytes into `format="base64"` (inline) or `format="file"` (temp path) MCP content |
 | `tools/perception.py` | Read-only queries: cameras, frames, tags, tag history, `where` |
-| `tools/configuration.py` | Whole-document camera/playfield config, and calibration trigger |
+| `tools/configuration.py` | Whole-document camera/playfield config, calibration trigger, and (sprint 037) flat-field gain-map capture/inspect/clear |
 | `tools/mobile_tags.py` | Mount-registry: `register_tag` / `unregister_tag` / `list_tag_parameters` |
 | `tools/annotations.py` | World-coordinate drawing/annotation-store surface: `draw_marker`/`draw_path`/`draw_polygon`/`draw_circle`/`draw_text`/`remove_annotation`/`clear_layer`/`list_annotations` |
 
@@ -111,6 +111,11 @@
   would pull `cv2` into `mcp/` transitively, violating the "cv2 absent
   from sys.modules after importing every mcp/ module" success
   criterion even though no tool here ever touches a camera.
+- **`capture_flatfield`/`get_flatfield`/`clear_flatfield` (sprint 037)
+  follow the identical rule**, using `calibration.interfaces.FlatFieldResult`/
+  `FlatFieldInfo` — never `calibration.flatfield.FlatFieldCapturer`'s
+  own module (which imports `cv2` at module scope, same as
+  `calibration.calibration`) — for exactly the same reason.
 - **`list_tag_parameters` and `list_annotations` differ in whether they
   need a flattening shim.** `Daemon.list_tag_parameters` returns a
   dataclass-keyed `dict`, which does not survive the MCP/JSON boundary
@@ -179,8 +184,12 @@
   mutates daemon state.
 - **Configuration** (`tools/configuration.py`) — `get_camera_config`,
   `set_camera_config`, `reset_camera_config`, `get_playfield`,
-  `set_playfield`, `list_playfields`, `calibrate`, `get_calibration`.
-  Whole-document push/fetch plus the calibration trigger.
+  `set_playfield`, `list_playfields`, `calibrate`, `get_calibration`,
+  and (sprint 037) `capture_flatfield` (`exposure`/`frames`/`force`/
+  `dry_run` optional parameters, mirroring `calibrate`'s
+  `estimate_intrinsics` shape), `get_flatfield`, `clear_flatfield`.
+  Whole-document push/fetch plus the calibration and flat-field-capture
+  triggers.
 - **Mobile tags** (`tools/mobile_tags.py`) — `register_tag`,
   `unregister_tag`, `list_tag_parameters`. In-memory mount-parameter
   registrations that do not survive a daemon restart.
@@ -203,8 +212,9 @@
   — `get_guide`'s direct, no-daemon dependency.
 - `aprilcam.config.loader.ConfigLoader` — `parse_camera_config`/
   `parse_playfield` for client-side document validation before any RPC.
-- `aprilcam.calibration.interfaces.Calibration` / `CalibrationResult`
-  — the cv2-free calibration types `configuration.py` returns.
+- `aprilcam.calibration.interfaces.Calibration` / `CalibrationResult` /
+  `FlatFieldResult` / `FlatFieldInfo` (sprint 037) — the cv2-free
+  calibration and flat-field types `configuration.py` returns.
 - `aprilcam.types` — the dataclass vocabulary (`CameraState`,
   `TagFrame`, `TagId`, `TagRecord`, `MountParameters`, `Annotation`,
   `AnnotationSet`, shape types) every tool builds or returns.
```
