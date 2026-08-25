---
source_file: config-DESIGN.md
source_hash: 4204a991265a0cc901aabcd0f5be47d88fa6ca2c488c4f049d05672c4222f7f4
---
# Diff: config-DESIGN.md

Comparison of the sprint overlay copy of `config-DESIGN.md` against its pristine (seed-commit) canonical version.

```diff
--- config-DESIGN.md (pristine)
+++ config-DESIGN.md (current)
@@ -71,9 +71,13 @@
   config.json` — a human-facing CLI tool, the sanctioned writer of
   `config_dir`), `camera/registry.py`'s `_save` (registry, `state_dir`),
   and `daemon/service.py`'s calibration-result writer (`calibration.json`,
-  `state_dir`). This is a deliberate absence, not an oversight — treat any
-  future write path added directly inside `config/` as suspicious unless
-  it is documented as an intentional boundary change.
+  `state_dir`). `daemon/service.py` gained a fourth such writer in sprint
+  036 — the mobile-tag mount registry (`state_dir/mounts/registry.json`),
+  the same atomic tmp+fsync+`os.replace` pattern as the other three (see
+  `daemon/DESIGN.md` §3/§4). This is a deliberate absence, not an
+  oversight — treat any future write path added directly inside `config/`
+  as suspicious unless it is documented as an intentional boundary
+  change.
 - **Files never migrate implicitly** (design.md §2 point 8). `directories.py`'s
   `_resolve_root`/`_resolve_socket_dir` re-run the flag > env > default >
   fallback cascade fresh on every construction; there is no code path that
```
