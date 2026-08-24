---
source_file: design.md
source_hash: f3ab6f01eed7d46819b4fc55b9387b4826d0eb94295b939b5cfc11b3f4fc76f0
---
# Diff: design.md

Comparison of the sprint overlay copy of `design.md` against its pristine (seed-commit) canonical version.

```diff
--- design.md (pristine)
+++ design.md (current)
@@ -58,11 +58,18 @@
 1. **Single vision authority.** All OpenCV work — capture, detection,
    tracking, homography, deskew, calibration — runs in the daemon
    process only. No other component opens a camera or imports cv2.
-   (Exception: `tags/` generation, an offline utility. Note §9's
-   packaging caveat — this is a runtime/import-boundary rule, enforced
+   (Exception: `tags/` generation, an offline utility — `cv2` is
+   importable everywhere, including a base install, but as of sprint
+   036 it is imported **lazily, at the point of use**, not at module
+   scope, and generating an actual tag image without the `daemon` extra
+   installed fails with a clear message rather than working. This
+   revises sprint 028's original recorded decision to keep
+   `opencv-contrib-python` a **permanent, unconditional** base
+   dependency for `tags/`'s sake — see `tags/DESIGN.md` §1 and §6 for
+   the reversal and its rationale. The import-boundary rule is enforced
    by convention in most subsystems and mechanically by test for
-   `client/`, `view/`, and `mcp/`; it does not currently determine which
-   `pip install` extra a dependency lives in.)
+   `client/`, `view/`, and `mcp/`; §9 no longer lists a packaging gap
+   here — see §9's revised item 1.)
 2. **Compute once, publish to all.** Any derived product needed by more
    than one client (deskewed image, tag stream) is computed in the
    daemon and published; clients subscribe, never recompute.
@@ -221,8 +228,9 @@
   cameras/registry.json            # identity -> enum + camera dir
   cameras/<cam>/calibration.json   # homography, static markers, provenance
   cameras/<cam>/info.json          # advisory: calibration_stale, frame_dark
-                                   # (no tags.json — mobile-tag registry is
-                                   #  in-memory; register at session start)
+  mounts/registry.json             # TagId -> MountParameters (sprint 036;
+                                   #  previously in-memory only, register at
+                                   #  session start -- see daemon/DESIGN.md §3)
 socket_dir/                        # control.sock + per-camera stream sockets
 ```
 
@@ -265,15 +273,18 @@
 Each is also recorded in its owning subsystem's own `DESIGN.md` under
 "Open Questions / Known Limitations":
 
-1. **OpenCV is currently a base dependency**, not gated behind the
-   `daemon` extra (`pyproject.toml`'s `dependencies`, not
-   `optional-dependencies.daemon`) — contradicting the target this
-   document and the project overview both state (the MCP server/CLI/
-   view client should need only Pillow/NumPy/mss/dotenv). This is what
-   currently makes it possible for `client/renderer.py` and
-   `cli/locate.py` to import `calibration/geometry.py` (which imports
-   `cv2` at module scope) without breaking at runtime; closing this
-   packaging gap as documented would break both without further work.
+1. **Resolved, sprint 036: OpenCV is now a `daemon`-extra-only
+   dependency.** `opencv-contrib-python` moved from `[project.dependencies]`
+   to `[project.optional-dependencies.daemon]`. This closed two related
+   gaps at once: `client/renderer.py` and `cli/locate.py` no longer
+   transitively require `cv2` at all (`calibration/geometry.py`'s two
+   genuinely `cv2`-dependent functions, `undistort_point`/`distort_point`,
+   moved to a new `calibration/distortion.py` — see `calibration/DESIGN.md`
+   §2 and Design Rationale), and `tags/generator.py`'s `cv2` import
+   became lazy, at the point of use, rather than module-scope (see
+   `tags/DESIGN.md` §1/§4/§6 for that reversal of sprint 028's original
+   "permanent base dependency" decision). A base-only install now
+   genuinely cannot import `cv2` anywhere in its reachable module graph.
 2. **No libcamera capture backend exists yet** — only `/sys`-based
    enumeration (`camera/backends/libcamera.py`) and the backend-selection
    *branch point* (`camera/backends/resolve_capture_backend`, sprint
@@ -292,7 +303,11 @@
    `clasi/issues/camera-probe-reports-everything-uncalibrated-and-renders-skipped-cameras-as-data.md`)
    — a `daemon/service.py` presentation bug, not a defect in `camera/`'s
    own `ProbeReport` contract.
-6. **`cli/`'s cv2-free boundary is not mechanically enforced**, unlike
-   `client/`/`view/`/`mcp/` — `cli/locate.py` transitively imports cv2
-   via `calibration/geometry.py`, accepted and documented but not
-   test-gated.
+6. **Resolved as a side effect of item 1, sprint 036: `cli/locate.py` no
+   longer imports cv2 transitively at all** (the `calibration/geometry.py`
+   split removed the accidental dependency, not a deliberate widening of
+   `tests/test_no_cv2_in_client_modules.py`'s enforced package set — see
+   `cli/DESIGN.md` §3/§6). `cli/` is still not in that test's mechanically
+   enforced `_CV2_FREE_PACKAGES` set, so a future change reintroducing a
+   `cv2` import into `cli/` would not be caught by that gate — this
+   remains a discipline/code-review boundary, not a test-gated one.
```
