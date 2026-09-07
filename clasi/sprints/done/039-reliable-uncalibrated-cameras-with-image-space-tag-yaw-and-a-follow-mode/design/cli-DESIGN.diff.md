---
source_file: cli-DESIGN.md
source_hash: c8c7a45b63d9aad377db76c4b11aa82974c42ae9f213309930c6bf3cd3f720f9
---
# Diff: cli-DESIGN.md

Comparison of the sprint overlay copy of `cli-DESIGN.md` against its pristine (seed-commit) canonical version.

```diff
--- cli-DESIGN.md (pristine)
+++ cli-DESIGN.md (current)
@@ -158,6 +158,30 @@
   fallback. This refusal shape (`cmd_view`'s in `camera.py`) is the
   template every later single-target command in this package copies
   verbatim, `flatfield.py` included.
+- **`camera tags --follow` is read-only but still refuses `--all`
+  (sprint 039) — a documented, narrow exception to "fan-out is
+  restricted to read-only commands," not a contradiction of it.** The
+  fan-out table's premise is a bounded one-shot RPC per target,
+  rendered and done; `--follow` is an unbounded blocking loop over one
+  stream. `fan_out()`'s per-target "call, render, move to the next
+  daemon" shape has no way to interleave N concurrently-blocking
+  streams, so `--follow --all` is refused up front, before any
+  connection, with a message pointing at running `--follow` once per
+  daemon — the same refusal shape (checked client-side, before any
+  RPC) every other `--all`-excluded command already uses, just for a
+  different underlying reason (unboundedness, not write-shape).
+- **`--json` on `camera tags --follow` is `cli/`'s first-ever
+  machine-readable output mode (sprint 039) — a narrow, explicit
+  exception to §6's "no `--json`/`--format` flag anywhere in this
+  package," not a reversal of it.** Scoped to this one flag on this one
+  command: every other command keeps printing human-aligned text only.
+  The exception is justified because `--follow`'s entire purpose is
+  recording a stream over time (the stakeholder's own use case), which
+  a human-aligned table is a poor fit for once a script wants to
+  consume it — `_format_tag_row`'s existing human-formatted string
+  stays the default; `--json` swaps in one JSON object per line with
+  the same fields, still going through the same `AprilCamError`
+  ->`report_error` handling for a connection failure.
 - **`camera flatfield`'s `--force`/`--dry-run` flags mirror `calibrate.py`'s
   existing `--with-intrinsics` opt-in-flag shape** (sprint 037): plain
   `store_true` flags, defaulted off, passed straight through as RPC
@@ -267,6 +291,27 @@
 the whole command, since there's nothing left to iterate — distinct
 from "enumerated zero," which is a normal empty result.
 
+**`camera tags --follow`/`--json` (sprint 039).** `cmd_tags` gains a
+`--follow` flag: instead of one `discovery.connect(args.daemon)
+.get_tags(args.camera)` call, it opens `daemon.stream_tags(args.camera)`
+(the client-side `TagStream` — `client.streams.SocketTagStream`,
+already used by nothing in this package before this sprint) and, for
+each yielded `TagFrame`, prints one `_format_tag_row`-shaped line per
+tag (extended to include `image_yaw_deg` for the uncalibrated branch,
+per `types.TagRecord`'s new field) prefixed with a timestamp, until a
+`KeyboardInterrupt` (Ctrl-C) — at which point it closes the stream
+(`TagStream.close()`, unsubscribing daemon-side on the next publish
+tick per `client/streams.py`'s own contract) and returns `EXIT_OK`, the
+same "Ctrl-C is a normal way to end a following command" convention
+`camera view`'s window-close path already establishes, not a crash.
+`--json` swaps `_format_tag_row`'s human line for one
+`json.dumps({...})` line per tag with the same fields (`tag`,
+`timestamp`, `pixel`, `image_yaw_deg`) — this package's first
+machine-readable output (§3). No new client-library method is needed:
+`GrpcDaemon.stream_tags` (`client/daemon_client.py`) already exists
+from the streaming sprint; this ticket is purely `cli/camera.py`
+consuming it.
+
 **Rejected alternative, recorded in code:** `daemon.py`'s module
 docstring records that `daemon status --all` was in-scope per
 `cli.md`'s general fan-out table but was deliberately deferred out of
@@ -320,8 +365,12 @@
   dependency, and the whole reason this package can stay thin:
   `client.discovery.Discovery` (`connect`, `find_all`),
   `client.lifecycle.DaemonLifecycle` (local-only process control),
-  `client.guides.Guides`. Every RPC-shaped call in this package is one
-  line into a `Daemon` handle `Discovery.connect()` returns.
+  `client.guides.Guides`, and — **sprint 039** — `Daemon.stream_tags`
+  (`client/daemon_client.py`, returning a `client.streams.SocketTagStream`)
+  for `camera tags --follow`, this package's first consumer of a stream
+  client rather than a one-shot RPC. Every RPC-shaped call in this
+  package is one line into a `Daemon` handle `Discovery.connect()`
+  returns.
 - **`daemon.interfaces`** — not the daemon implementation (`cli/`
   never runs daemon code or touches cv2 through it), but the shared,
   cv2-free `Daemon`/`DaemonStatus`/`MountRegistration` interface and
@@ -350,11 +399,16 @@
 ## 6. Open Questions / Known Limitations
 
 - **`camera flatfield`'s output format (spread-before/after reporting) has no machine-readable mode either** — same limitation as every other command in this package (below); an agent driving this workflow through the MCP tool instead of the CLI gets the structured `FlatFieldResult` directly.
-- **No machine-readable output mode.** Every command prints
-  human-aligned text; there is no `--json`/`--format` flag anywhere in
-  this package, so scripting against `cli/` output means parsing the
-  same text a human reads (`fan_out`'s exit code is the one thing
-  designed for script consumption).
+- **No machine-readable output mode, except `camera tags --follow
+  --json` (sprint 039).** Every other command still prints
+  human-aligned text only; there is no general `--json`/`--format` flag
+  across the package, so scripting against most of `cli/`'s output
+  still means parsing the same text a human reads (`fan_out`'s exit
+  code remains the one thing designed for script consumption on those
+  commands). Whether to generalize `--json` to other commands (`camera
+  tags` one-shot, `camera list`, ...) is an open call for a future
+  sprint, not decided here — this sprint's scope is narrowly the
+  streaming-recording use case `--follow` serves.
 - **`cli/` is still not enforced cv2-free**, unlike `client/`, `view/`,
   and `mcp/` (`tests/test_no_cv2_in_client_modules.py`'s
   `_CV2_FREE_PACKAGES`) — even though, as of sprint 036, no file in this
```
