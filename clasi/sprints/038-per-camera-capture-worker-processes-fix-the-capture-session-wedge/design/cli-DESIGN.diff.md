---
source_file: cli-DESIGN.md
source_hash: d90e355c777e71a8a59522ea6017c2672d16b479a04367192250d50d900dc8e8
---
# Diff: cli-DESIGN.md

Comparison of the sprint overlay copy of `cli-DESIGN.md` against its pristine (seed-commit) canonical version.

```diff
--- cli-DESIGN.md (pristine)
+++ cli-DESIGN.md (current)
@@ -99,6 +99,20 @@
   title for the whole session — lives in `view/viewer.py`'s
   `run_raw_view`, not in this subsystem; this package's contribution
   is the one printed line.
+- **`camera view` fails within a bounded timeout instead of hanging
+  forever when no frame ever arrives (sprint 038).** `cmd_view` now
+  catches `view.viewer.FirstFrameTimeout` (`view-DESIGN.md`'s new,
+  client-local, non-`AprilCamError` exception) alongside the existing
+  `viewer.NoDisplayAvailable`/`AprilCamError` catches, and prints a
+  diagnostic naming the camera and the likely cause (a wedged capture
+  session — pointing at the same remediation `camera-DESIGN.md`'s
+  worker-process fix targets) before returning `EXIT_ERROR`, instead of
+  the process hanging with the window open and no error at all. This is
+  the client-visible half of the original issue's gap 2; the exact same
+  refusal shape `cmd_view`'s existing `--all` refusal and
+  `NoDisplayAvailable` handling already establish for this function —
+  catch a typed condition, print one clear line, return `EXIT_ERROR` —
+  extended to a third condition rather than introducing a new pattern.
 - **`camera locate` is the one file that touches non-daemon vision
   code, deliberately and explicitly.** Every other command triggers an
   RPC and formats the result. `locate.py` does the same for detection
@@ -321,7 +335,8 @@
   `playfield view` hand a connected `Daemon` to; this package's own
   contribution is resolving the camera/daemon and reporting
   `viewer.NoDisplayAvailable`/`errors.NotCalibrated` with a
-  command-specific pointer.
+  command-specific pointer — and, **sprint 038**,
+  `viewer.FirstFrameTimeout` with the same treatment.
 - **`calibration.geometry`** — `locate.py` only, for
   `solve_camera_geometry`/`ReferenceObservation` (§3's documented
   exception).
```
