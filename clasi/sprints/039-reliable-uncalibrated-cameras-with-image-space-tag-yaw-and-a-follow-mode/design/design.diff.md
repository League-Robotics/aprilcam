---
source_file: design.md
source_hash: 7d4673061215df045e59e87de77560cd993696f8283f951d2c414a7cb798e1b1
---
# Diff: design.md

Comparison of the sprint overlay copy of `design.md` against its pristine (seed-commit) canonical version.

```diff
--- design.md (pristine)
+++ design.md (current)
@@ -200,6 +200,22 @@
 - **Tag yaw:** the direction the tag's front faces; 0 = east, CCW
   positive (same convention as heading). Yaw is orientation, heading
   is motion — a robot reversing has them 180° apart.
+- **Image-space yaw (sprint 039) is a separate, diagnostic-only field
+  with a different frame and a different sign convention — do not
+  conflate it with the bullet above.** `TagRecord.image_yaw_deg` is
+  derived from the same `corners[0]→corners[1]` front edge, but in raw
+  pixel space: 0° = the tag's front edge toward the **top of the
+  image**, clockwise-positive **as seen on screen** (image y grows
+  downward, so this is the opposite rotational sense from the
+  mathematical CCW-positive convention every world-frame angle in this
+  document uses). It is populated for every `TagRecord` that carries
+  `pixel_corners` — calibrated or not — specifically so an uncalibrated
+  camera (no world frame to report `yaw_rad` in at all) still reports
+  *some* orientation. It never substitutes for `yaw_rad`, is never
+  computed from world corners, and existed for exactly zero sprints
+  before this one — 5a's "raw-pixel fields are diagnostic-only" rule is
+  what licenses adding a pixel-space field here rather than that rule
+  being a historical accident this sprint quietly works around.
 - **Corner order:** tag boundary quads are `(front-left, front-right,
   back-right, back-left)` in the tag's own frame; the corner 0→1 edge
   is the front edge. Same physical order in world and pixel space.
@@ -336,3 +352,21 @@
    `cli/DESIGN.md` §3). Whether the wedge correlates with UVC control
    writes during an active stream remains genuinely open (carried
    forward in `camera/DESIGN.md` §6) but no longer blocks the fix.
+8. **Resolved, sprint 039: camera reopen (self-heal and the stall
+   watchdog) no longer reuses a cached OpenCV index.** Both now
+   re-resolve the index from `hardware_key` via a fresh `resolve_all()`
+   immediately before every open — not only the first one — closing a
+   silent camera-substitution failure mode: on an overloaded bus, a
+   sibling camera leaving/rejoining could shift AVFoundation's
+   positional indices enough that a reopen retried a now-different
+   physical camera under the original slot's name, with nothing in the
+   output indicating the substitution (`camera/DESIGN.md` §4's new
+   "Identity-safe reopen" subsection). A hardware key not currently
+   enumerated now reports `present=False`/`usable=False` and is
+   retried by identity, never by a stale index. Related but distinct
+   from item 5 above (which is a presentation/overlay bug in
+   calibration/playfield state, not a health-reporting bug): this
+   sprint also makes `camera probe`/`camera list` report an
+   actively-held camera's *real* health (heartbeat age, reopen budget)
+   instead of an unconditional `usable=True` for any active slug
+   (`camera/DESIGN.md` §4, `daemon/DESIGN.md`'s update).
```
