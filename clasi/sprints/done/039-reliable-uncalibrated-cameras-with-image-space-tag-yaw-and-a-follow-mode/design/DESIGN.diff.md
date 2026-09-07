---
source_file: DESIGN.md
source_hash: de5517553ac161b590a7731f3a02569dc2afa2fe446ca6ef7ad9165fd0381161
---
# Diff: DESIGN.md

Comparison of the sprint overlay copy of `DESIGN.md` against its pristine (seed-commit) canonical version.

```diff
--- DESIGN.md (pristine)
+++ DESIGN.md (current)
@@ -64,7 +64,11 @@
 - **`types.py`** — the shared value vocabulary (`TagId`, `PixelPoint`,
   `WorldPoint`, `TagFamily`, …) that every subsystem's `interfaces.py` is
   defined in terms of. Encodes the world-frame and heading conventions
-  above as executable types, not just prose.
+  above as executable types, not just prose. **Sprint 039** adds
+  `TagRecord.image_yaw_deg`, a diagnostic-only image-pixel-space field
+  distinct from the world-frame `yaw_rad` above — see `docs/design/
+  design.md` §6 for the sign convention and why it exists as a second,
+  separate field rather than an alternate reading of `yaw_rad`.
 - **`errors.py`** — the client-visible error taxonomy. Every distinction a
   use case draws about failure is a type here, not a message string, so an
   adapter (CLI, MCP) can dispatch on the exception type rather than
```
