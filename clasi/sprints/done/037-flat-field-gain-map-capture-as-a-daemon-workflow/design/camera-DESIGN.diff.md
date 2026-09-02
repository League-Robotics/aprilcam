---
source_file: camera-DESIGN.md
source_hash: 012955962068b4eb73d83960c11b7b47763e9297f888f2c755d66f2b814fac1d
---
# Diff: camera-DESIGN.md

Comparison of the sprint overlay copy of `camera-DESIGN.md` against its pristine (seed-commit) canonical version.

```diff
--- camera-DESIGN.md (pristine)
+++ camera-DESIGN.md (current)
@@ -72,9 +72,12 @@
 - **`camera.py`** — `Camera`: the runtime object the daemon holds
   one-of-per-camera. Composes a primary `CaptureBackend` (pixels) with
   an optional `ControlBackend` (settings), and owns self-heal reopen,
-  whole-document `apply_config` with rollback, and the
+  whole-document `apply_config` with rollback, the
   `effective_config`/`reset`/`stored_config` trio for the config-service
-  boundary.
+  boundary, and — an out-of-process change predating this doc's last
+  review, documented here for the first time — applying a per-camera
+  flat-field gain map (`FLATFIELD_FILENAME`, `_load_flatfield`/
+  `_apply_flatfield`; see §4 "Flat-field gain-map application" below).
 - **`backends/`** — one small adapter per capture technology, plus the
   shared `resolve_capture_backend()` branch point:
   - `__init__.py` — `CaptureBackend` (open/read/close/frame_size) and
@@ -313,6 +316,32 @@
 `threading.Lock` around `_known_hardware_keys`/`_present_slugs`, updated
 by both `probe()` and the background `watch()` poll thread.
 
+### Flat-field gain-map application (out-of-process, documented here for the first time; capture side is sprint 037)
+
+`Camera` optionally accepts a `flatfield_path` at construction (wired by
+`daemon/service.py`'s `_flatfield_path_for(slug)`, `<state_dir>/cameras/
+<slug>/flatfield.npy`, right next to `calibration.json` — a measured
+artifact tied to this camera's current mounting and lighting, like a
+calibration, not an operator-authored setting). `_load_flatfield` reads
+it (if present) on every `open()`/`reset()` — never on construction alone
+— accepting a finite, non-negative float `(H, W)` or `(H, W, C)` array
+and rejecting anything else by name, never fatally: a malformed or
+unreadable map is logged and ignored, since a camera must never become
+unopenable over an optional correction artifact. `_apply_flatfield`
+multiplies every frame `read()` returns by the loaded map and saturates
+back to the frame's own dtype; a map whose `(H, W)` no longer matches the
+frame's (a resolution change since capture) is skipped with one
+rate-limited warning rather than silently mis-corrected. This class has
+**no notion of the map's provenance or staleness** — `captured_at`,
+`stale`, and gain-range reporting are daemon-level state (sprint 037's
+`FlatFieldInfo`, held in `DaemonService._flatfields`), the identical
+division of responsibility §3/§4 already establish for `calibrated`/
+`calibration_stale` on `CameraState`. `Camera` itself changes not at all
+for sprint 037's capture workflow — install/replace/clear are exactly the
+same "file present or not, reloaded on `open()`/`reset()`" mechanism the
+apply side already shipped with; the daemon just gained a way to *produce*
+the file instead of an operator hand-running a script.
+
 ### Rejected/changed alternatives, per the code's own record
 
 - **Generic `cv2.VideoCapture.set()` as the settings surface** — kept as
@@ -353,7 +382,8 @@
   never omits the camera), `.watch()` (idempotent background hotplug
   polling), `.known_present() -> FrozenSet[str]`, `.stop_watch()`.
 - **`camera.Camera(slug, backend, *, control_backend=, device_name=,
-  number=, playfield=, config_loader=, config_path=, on_reopen=)`** —
+  number=, playfield=, config_loader=, config_path=, on_reopen=,
+  flatfield_path=)`** —
   `.slug`, `.state -> CameraState`, `.open()`/`.close()` (raise on
   failure), `.read() -> Optional[np.ndarray]` (never raises),
   `.apply_config(config: CameraConfig)` (whole-push, hard-errors on an
@@ -403,6 +433,7 @@
 
 ## 6. Open Questions / Known Limitations
 
+- **The flat-field apply side (§4) shipped out-of-process and went undocumented in this file until sprint 037's planning pass caught the drift.** No functional gap — the code and its own docstrings were always complete — but a reminder that an out-of-process change is still expected to update the owning subsystem's `DESIGN.md`, and this one didn't at the time.
 - **No libcamera capture backend exists yet.** `backends/libcamera.py`
   and `backends/__init__.py` both say so explicitly: only `/sys`-based
   enumeration is ported; v1's `cam`-tool/GStreamer "gst mode" and an
```
