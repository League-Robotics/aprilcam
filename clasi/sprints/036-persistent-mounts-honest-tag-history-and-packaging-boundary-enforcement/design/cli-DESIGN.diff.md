---
source_file: cli-DESIGN.md
source_hash: 256154c7de8fde9f15439d671eeb336a29b2ee584bdae142705ebdeaf0123d04
---
# Diff: cli-DESIGN.md

Comparison of the sprint overlay copy of `cli-DESIGN.md` against its pristine (seed-commit) canonical version.

```diff
--- cli-DESIGN.md (pristine)
+++ cli-DESIGN.md (current)
@@ -108,16 +108,20 @@
   already-vision-processed `WorldPoint`s — no cv2, no pixel access, no
   camera (sprint 029's Design Rationale: "client-side pure function,
   not a new `LocateCamera` RPC"). This is `cli/`'s first-ever
-  dependency on `calibration/`. One wrinkle worth flagging: the
-  imported function lives in `calibration/geometry.py`, a module that
-  *does* `import cv2 as cv` at module scope (for unrelated
-  `undistort_point`/`distort_point` helpers elsewhere in that file),
-  so importing `locate.py` transitively pulls cv2 into the `cli/`
-  process even though `solve_camera_geometry` itself never calls it.
-  `tests/test_no_cv2_in_client_modules.py`'s `_CV2_FREE_PACKAGES`
-  enforces a hard "no cv2" import ban on `client/`, `view/`, and
-  `mcp/` — `cli/` is *not* in that set, so this transitive import is
-  currently unchecked (see §6).
+  dependency on `calibration/`. **Resolved, sprint 036**: the imported
+  function used to live in `calibration/geometry.py` alongside an
+  unrelated top-level `import cv2 as cv` (for `undistort_point`/
+  `distort_point`), so importing `locate.py` transitively pulled `cv2`
+  into the `cli/` process even though `solve_camera_geometry` itself
+  never called it. That `cv2` import moved to a new sibling module,
+  `calibration/distortion.py` (see `calibration/DESIGN.md` §2/Design
+  Rationale) — `geometry.py` is now genuinely `cv2`-free, so
+  `locate.py`'s import of `solve_camera_geometry` no longer pulls in
+  `cv2` at all. `tests/test_no_cv2_in_client_modules.py`'s
+  `_CV2_FREE_PACKAGES` still does not include `cli/` (see §6) — this fix
+  removed the one known transitive leak, but `cli/` staying `cv2`-free
+  going forward is still a discipline/code-review matter, not a
+  test-gated one.
 - **`guide` makes no daemon connection at all, on purpose.** Every
   other module connects via `client.discovery.Discovery` and accepts
   `-d`/`--daemon`. `guide.py` does neither: UC-27.S-1 requires the
@@ -323,16 +327,15 @@
   this package, so scripting against `cli/` output means parsing the
   same text a human reads (`fan_out`'s exit code is the one thing
   designed for script consumption).
-- **`cli/` is not enforced cv2-free**, unlike `client/`, `view/`, and
-  `mcp/` (`tests/test_no_cv2_in_client_modules.py`'s
-  `_CV2_FREE_PACKAGES`). `locate.py`'s import of
-  `calibration.geometry` is today the one path by which cv2 enters the
-  `cli/` process (transitively, via that module's unrelated
-  `undistort_point`/`distort_point` helpers — `solve_camera_geometry`
-  itself never calls cv2). This is called out explicitly in
-  `locate.py`'s own docstring as accepted, not accidental, but it
-  means the vision-boundary guarantee for this package rests on
-  discipline and code review, not a test gate.
+- **`cli/` is still not enforced cv2-free**, unlike `client/`, `view/`,
+  and `mcp/` (`tests/test_no_cv2_in_client_modules.py`'s
+  `_CV2_FREE_PACKAGES`) — even though, as of sprint 036, no file in this
+  package transitively imports `cv2` today (`locate.py`'s former leak via
+  `calibration/geometry.py` is closed; see §3). Nothing prevents a future
+  change from reintroducing one, since this package is not in the AST
+  scan's enforced set. Whether to add `cli/` to `_CV2_FREE_PACKAGES` now
+  that it is actually clean is an open call for a future sprint, not
+  decided here.
 - **`_unwritable_reason` is duplicated** between `camera.py` and
   `playfield.py` rather than shared via `_common.py` — a deliberate
   choice per each module's docstring, but it means the two copies can
```
