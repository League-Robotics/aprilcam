---
source_file: cli-DESIGN.md
source_hash: a85f5506b51f93f16ebd505a917410ec1522ea68587199353b397f0f9a9f4a47
---
# Diff: cli-DESIGN.md

Comparison of the sprint overlay copy of `cli-DESIGN.md` against its pristine (seed-commit) canonical version.

```diff
--- cli-DESIGN.md (pristine)
+++ cli-DESIGN.md (current)
@@ -43,6 +43,7 @@
 | `_common.py` | 257 | Shared plumbing: `EXIT_OK`/`EXIT_ERROR`, `add_daemon_option`/`add_all_option` (the `-d`/`--all` flags), `incomplete_command` (a subparser-tree default), `report_error`, `format_camera_row`/`print_cameras`/`print_probe_report`, `fan_out`, `resolve_playfield_camera`. |
 | `camera.py` | 521 | `camera list\|probe\|image\|tags\|config\|view` — camera-addressed commands. Also wires in `calibrate.py` and `locate.py` as sub-subcommands of its own `camera` subparser tree. |
 | `calibrate.py` | 146 | `camera calibrate` — a pure RPC orchestrator around the `Calibrate` RPC; grouped under `camera` but kept in its own file. |
+| `flatfield.py` | ~130 (sprint 037) | `camera flatfield` — a pure RPC orchestrator around `CaptureFlatField`/`GetFlatField`/`ClearFlatField`; grouped under `camera`, same one-file shape as `calibrate.py`. |
 | `locate.py` | 431 | `camera locate` — solves camera ground nadir/height from surveyed AprilTag references. The one file in this package with a client-side geometry dependency (§3). |
 | `daemon.py` | 313 | `daemon start\|stop\|restart\|status\|list` — process lifecycle (local-only) plus remote status/discovery. |
 | `playfield.py` | 688 | `playfield list\|config\|image\|view\|annotations\|where` — playfield-addressed commands, including client-side camera resolution for the ones that need a camera but name none. |
@@ -50,7 +51,7 @@
 | `guide.py` | 75 | `guide [name]` — prints a packaged usage guide. Deliberately makes no daemon connection at all (§3). |
 | `mcp.py` | 54 | `mcp [-d HOST]` — starts the MCP stdio server; passes `-d` through as the server's connection-manager default and blocks for the process lifetime. |
 
-Total: ~3,100 lines across 9 modules (no `interfaces.py` — see §5).
+Total: ~3,230 lines across 10 modules (no `interfaces.py` — see §5).
 
 **Dispatch mechanism.** `cli/__init__.py:_build_parser()` builds one
 top-level `argparse.ArgumentParser` and calls each subcommand module's
@@ -61,9 +62,9 @@
 subparser tree entered without a leaf command (e.g. `aprilcam daemon`)
 falls through to `_common.incomplete_command(parser)`'s default, which
 prints that parser's help and returns `EXIT_ERROR` rather than
-succeeding silently. `calibrate.py` and `locate.py` are grouped under
-`camera` structurally (their `add_subparser` takes `camera.py`'s own
-`sub` subparsers action, not the top-level one) while remaining
+succeeding silently. `calibrate.py`, `locate.py`, and (sprint 037) `flatfield.py` are grouped
+under `camera` structurally (their `add_subparser` takes `camera.py`'s
+own `sub` subparsers action, not the top-level one) while remaining
 separate files — the same "one thin module, hooked in by its parent"
 shape `playfield.py` uses at the top level.
 
@@ -135,13 +136,24 @@
   with nothing to target, so it takes no `-d`/`--all` flags at all.
 - **Fan-out (`--all`) is restricted to read-only commands**
   (`docs/design/use-cases/cli.md` §"Fan-out"). Every write-shaped
-  command (`camera calibrate`, `camera locate`, `camera view`,
-  `playfield view`, `camera config` push, `playfield annotations
-  --clear`, `tags mount register`/`clear`) refuses `--all` up front,
-  before any connection is attempted, with a message naming the
-  restriction — never a silent single-target fallback. This refusal
-  shape (`cmd_view`'s in `camera.py`) is the template every later
-  single-target command in this package copies verbatim.
+  command (`camera calibrate`, `camera flatfield` (sprint 037), `camera
+  locate`, `camera view`, `playfield view`, `camera config` push,
+  `playfield annotations --clear`, `tags mount register`/`clear`)
+  refuses `--all` up front, before any connection is attempted, with a
+  message naming the restriction — never a silent single-target
+  fallback. This refusal shape (`cmd_view`'s in `camera.py`) is the
+  template every later single-target command in this package copies
+  verbatim, `flatfield.py` included.
+- **`camera flatfield`'s `--force`/`--dry-run` flags mirror `calibrate.py`'s
+  existing `--with-intrinsics` opt-in-flag shape** (sprint 037): plain
+  `store_true` flags, defaulted off, passed straight through as RPC
+  request fields with no client-side interpretation — the daemon alone
+  decides what they mean (which quality-gate checks `force` overrides;
+  `dry_run` skips the persist/reload step). `--clear` is a fourth,
+  mutually-exclusive-in-practice mode (checked client-side before any
+  RPC: naming both `--clear` and a capture-only flag like `--exposure`
+  is a `report_error`-shaped usage error, never a silent
+  last-flag-wins).
 - **Destination/precondition checks run before any RPC that would
   cost a resource.** `camera image`/`playfield image` check output
   writability and terminal-attachment *before* calling `get_frame` —
@@ -270,7 +282,7 @@
 
 ```
 aprilcam daemon   start|stop|restart|status|list
-aprilcam camera   list|probe|image|tags|config|view|calibrate|locate
+aprilcam camera   list|probe|image|tags|config|view|calibrate|flatfield|locate
 aprilcam playfield list|config|image|view|annotations|where
 aprilcam tags     mount register|clear|list | generate
 aprilcam guide    [name]
@@ -322,6 +334,7 @@
 
 ## 6. Open Questions / Known Limitations
 
+- **`camera flatfield`'s output format (spread-before/after reporting) has no machine-readable mode either** — same limitation as every other command in this package (below); an agent driving this workflow through the MCP tool instead of the CLI gets the structured `FlatFieldResult` directly.
 - **No machine-readable output mode.** Every command prints
   human-aligned text; there is no `--json`/`--format` flag anywhere in
   this package, so scripting against `cli/` output means parsing the
```
