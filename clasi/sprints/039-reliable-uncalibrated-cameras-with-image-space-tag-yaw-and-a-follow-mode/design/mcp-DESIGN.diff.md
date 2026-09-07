---
source_file: mcp-DESIGN.md
source_hash: fb9d668f763b9ada5bebf414745ecaf108b200e38d45fab966de408edf1ebdfe
---
# Diff: mcp-DESIGN.md

Comparison of the sprint overlay copy of `mcp-DESIGN.md` against its pristine (seed-commit) canonical version.

```diff
--- mcp-DESIGN.md (pristine)
+++ mcp-DESIGN.md (current)
@@ -181,7 +181,14 @@
 - **Perception** (`tools/perception.py`) — `list_cameras`,
   `probe_cameras`, `get_frame` (raw or deskewed, base64/file),
   `get_tags`, `get_tag`, `get_tag_history`, `where`. Read-only; never
-  mutates daemon state.
+  mutates daemon state. **Sprint 039**: `get_tags`/`get_tag`/
+  `get_tag_history` carry `TagRecord`'s new `image_yaw_deg` field with
+  no code change in this file — none of the three needs a `.to_dict()`
+  shim (§3's "fetch tools return `to_dict()`-equivalent shapes" rule
+  only applies where the wire shape has *diverged* from the dataclass
+  shape; `TagRecord` has not), so FastMCP's pydantic introspection
+  picks up the new field automatically, the same way it already picks
+  up every other `TagRecord` field.
 - **Configuration** (`tools/configuration.py`) — `get_camera_config`,
   `set_camera_config`, `reset_camera_config`, `get_playfield`,
   `set_playfield`, `list_playfields`, `calibrate`, `get_calibration`,
```
