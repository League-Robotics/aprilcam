# cli/

**Owner:** Eric Busboom · **Last reviewed:** 2026-10-16 · **Status:** stable

---

## 1. Purpose

`cli/` is the `aprilcam` command-line entry point: the argparse dispatcher
plus one thin module per subcommand. It is a pure adapter in the sense
`docs/design/adapters.md` §1 defines the term — "a translation layer and
nothing more" between argv/exit-codes and the daemon's RPC contract. It
holds no authoritative state and does no vision (`adapters.md` §1's rule,
quoted verbatim in nearly every module docstring in this package).

Concretely, `cli/` owns:

- **Argument parsing** — one `argparse` subparser tree per noun
  (`daemon`, `camera`, `playfield`, `tags`, `guide`, `mcp`), each
  subcommand module building its own leaf parsers.
- **Output formatting** — turning whatever the client library returns
  (`CameraState`, `TagFrame`, `Playfield`, `AnnotationSet`, ...) into
  aligned human-readable lines, with a consistent `(no X)` convention
  for empty results.
- **Exit codes** — mapping a successful call to 0 and a typed
  `aprilcam.errors.AprilCamError` to 1, uniformly, via
  `cli/_common.py`'s `report_error`.

Every actual capability — camera enumeration, calibration, deskew,
tag detection, annotation storage — lives in the daemon and is reached
exclusively through `client/`. `cli/` never opens a camera, never talks
gRPC directly, and (with one narrow, explicitly-recorded exception in
`locate.py` — see §3) never touches vision/geometry code itself. This
is what makes it a subsystem boundary: everything below this layer is
daemon or client-library responsibility; everything at this layer is
presentation.

## 2. Orientation

| File | Lines | Owns |
|---|---|---|
| `__init__.py` | 160 | `main()` — the single dispatch seam. Builds the top-level parser, wires every subcommand module's `add_subparser`, resets `SIGPIPE` to default disposition, and is the one place `BrokenPipeError` is caught. |
| `_common.py` | 257 | Shared plumbing: `EXIT_OK`/`EXIT_ERROR`, `add_daemon_option`/`add_all_option` (the `-d`/`--all` flags), `incomplete_command` (a subparser-tree default), `report_error`, `format_camera_row`/`print_cameras`/`print_probe_report`, `fan_out`, `resolve_playfield_camera`. |
| `camera.py` | 521 | `camera list\|probe\|image\|tags\|config\|view` — camera-addressed commands. Also wires in `calibrate.py` and `locate.py` as sub-subcommands of its own `camera` subparser tree. |
| `calibrate.py` | ~210 | `camera calibrate [--verbose]` — a pure RPC orchestrator around the `Calibrate` RPC; grouped under `camera` but kept in its own file. Prints the matched ids, `lens model:` line, then (sprint 045) `method`, `confidence`, corners used, reprojection error in px and cm, scale agreement, angular spread, camera height/nadir/tilt and the worst leave-one-out marker; caveats (`reduced`/`low`, centre-point fallback reason, helpful markers not seen) go to stderr, `--verbose` adds the per-marker leave-one-out table, and a failed pose attempt prints its measurements before the reason. `--with-intrinsics` was removed in sprint 045. `camera list` shows `calibrated (pose, full)`; `camera lens` aliases `camera intrinsics`; `locate.py` prints the pose-vs-locate comparison note. |
| `flatfield.py` | ~130 (sprint 037) | `camera flatfield` — a pure RPC orchestrator around `CaptureFlatField`/`GetFlatField`/`ClearFlatField`; grouped under `camera`, same one-file shape as `calibrate.py`. |
| `locate.py` | 431 | `camera locate` — solves camera ground nadir/height from surveyed AprilTag references. The one file in this package with a client-side geometry dependency (§3). |
| `daemon.py` | 313 | `daemon start\|stop\|restart\|status\|list` — process lifecycle (local-only) plus remote status/discovery. |
| `playfield.py` | 688 | `playfield list\|config\|image\|view\|annotations\|where` — playfield-addressed commands, including client-side camera resolution for the ones that need a camera but name none. |
| `tags.py` | 429 | `tags mount register\|clear\|list` (mobile-tag mount registry, daemon-backed) and `tags generate` (offline AprilTag/ArUco printable PDF sheet, or per-id PNGs with `--png`; no daemon). |
| `guide.py` | 75 | `guide [name]` — prints a packaged usage guide. Deliberately makes no daemon connection at all (§3). |
| `mcp.py` | 54 | `[-d HOST] mcp` — starts the MCP stdio server; passes `-d` through as the server's connection-manager default and blocks for the process lifetime. |

Total: ~3,230 lines across 10 modules (no `interfaces.py` — see §5).

**Dispatch mechanism.** `cli/__init__.py:_build_parser()` builds one
top-level `argparse.ArgumentParser` and calls each subcommand module's
`add_subparser(subparsers)`. Every subcommand module owns its own
argparse shape and `cmd_*` functions; `__init__.py` itself "only
assembles them and routes `argv` to whichever `func` argparse
resolved — itself zero business logic" (its own module docstring). A
subparser tree entered without a leaf command (e.g. `aprilcam daemon`)
falls through to `_common.incomplete_command(parser)`'s default, which
prints that parser's help and returns `EXIT_ERROR` rather than
succeeding silently. `calibrate.py`, `locate.py`, and (sprint 037) `flatfield.py` are grouped
under `camera` structurally (their `add_subparser` takes `camera.py`'s
own `sub` subparsers action, not the top-level one) while remaining
separate files — the same "one thin module, hooked in by its parent"
shape `playfield.py` uses at the top level.

## 3. Constraints and Invariants

- **No vision, no daemon logic, no authoritative state** — every
  `cmd_*` function either shapes argv, calls exactly one (or a small,
  fixed sequence of) client-library methods, or formats an
  already-computed result. This is `adapters.md` §1's rule, restated
  in nearly every module docstring here rather than left implicit.
- **One dispatch seam for `BrokenPipeError` (ticket 031-003,
  SUC-003).** `main()` in `__init__.py` is the single point every
  subcommand's `func(args)` funnels through, so pipe-safety handling
  (`_reset_sigpipe_to_default` + the `except BrokenPipeError` wrapping
  `func(args)` + `_handle_broken_pipe`'s stdout-to-`/dev/null` redirect
  before interpreter shutdown) lives there once instead of at each of
  the ~20 individual `print()` call sites scattered across `cli/*.py`.
  Two-layer mechanism: `SIGPIPE` is reset to `SIG_DFL` at startup so
  the OS can kill the process outright on a broken-pipe write before
  any Python exception machinery runs; the `except BrokenPipeError`
  in `main()` is belt-and-suspenders for the cases where a
  `BrokenPipeError` still reaches Python (a write racing past the
  signal, or a context that doesn't deliver `SIGPIPE` the same way).
- **`camera view` is signposted as raw (ticket 031-004, SUC-004).**
  `camera.py`'s `cmd_view` prints a startup line — before the daemon
  connection is even attempted, so it is seen even if the connection
  later fails — naming the view `RAW` and pointing at
  `playfield view` as the deskewed alternative. This is purely a
  visibility fix; UC-16's underlying "raw means raw" guarantee (no
  deskew/homography applied by `camera view`) is unchanged. The
  companion half — an explicit `[RAW]` marker baked into the window
  title for the whole session — lives in `view/viewer.py`'s
  `run_raw_view`, not in this subsystem; this package's contribution
  is the one printed line.
- **`camera view` fails within a bounded timeout instead of hanging
  forever when no frame ever arrives (sprint 038).** `cmd_view` now
  catches `view.viewer.FirstFrameTimeout` (`view-DESIGN.md`'s new,
  client-local, non-`AprilCamError` exception) alongside the existing
  `viewer.NoDisplayAvailable`/`AprilCamError` catches, and prints a
  diagnostic naming the camera and the likely cause (a wedged capture
  session — pointing at the same remediation `camera-DESIGN.md`'s
  worker-process fix targets) before returning `EXIT_ERROR`, instead of
  the process hanging with the window open and no error at all. This is
  the client-visible half of the original issue's gap 2; the exact same
  refusal shape `cmd_view`'s existing `--all` refusal and
  `NoDisplayAvailable` handling already establish for this function —
  catch a typed condition, print one clear line, return `EXIT_ERROR` —
  extended to a third condition rather than introducing a new pattern.
- **`camera locate` is the one file that touches non-daemon vision
  code, deliberately and explicitly.** Every other command triggers an
  RPC and formats the result. `locate.py` does the same for detection
  (`GetTag`, entirely daemon-side), but the least-squares geometry
  *solve* itself
  (`aprilcam.calibration.geometry.solve_camera_geometry`) runs
  client-side, here, because it is pure NumPy algebra over
  already-vision-processed `WorldPoint`s — no cv2, no pixel access, no
  camera (sprint 029's Design Rationale: "client-side pure function,
  not a new `LocateCamera` RPC"). This is `cli/`'s first-ever
  dependency on `calibration/`. **Resolved, sprint 036**: the imported
  function used to live in `calibration/geometry.py` alongside an
  unrelated top-level `import cv2 as cv` (for `undistort_point`/
  `distort_point`), so importing `locate.py` transitively pulled `cv2`
  into the `cli/` process even though `solve_camera_geometry` itself
  never called it. That `cv2` import moved to a new sibling module,
  `calibration/distortion.py` (see `calibration/DESIGN.md` §2/Design
  Rationale) — `geometry.py` is now genuinely `cv2`-free, so
  `locate.py`'s import of `solve_camera_geometry` no longer pulls in
  `cv2` at all. `tests/test_no_cv2_in_client_modules.py`'s
  `_CV2_FREE_PACKAGES` still does not include `cli/` (see §6) — this fix
  removed the one known transitive leak, but `cli/` staying `cv2`-free
  going forward is still a discipline/code-review matter, not a
  test-gated one.
- **`guide` makes no daemon connection at all, on purpose.** Every
  other module connects via `client.discovery.Discovery` and accepts
  `-d`/`--daemon`. `guide.py` does neither: UC-27.S-1 requires the
  guide to be readable with no daemon running anywhere, so it reads
  straight from `client.guides.Guides`, which touches only the
  package's bundled text. Its own docstring warns explicitly against
  "fixing" this to route through `Discovery`/`Daemon` like everything
  else — that would defeat the point.
- **`tags generate` is the other daemon-free command**, for a
  different reason: it is a purely offline operation (`TagGenerator`)
  with nothing to target, so it takes no `-d`/`--all` flags at all.
- **Fan-out (`--all`) is restricted to read-only commands**
  (`docs/design/use-cases/cli.md` §"Fan-out"). Every write-shaped
  command (`camera calibrate`, `camera flatfield` (sprint 037), `camera
  locate`, `camera view`, `playfield view`, `camera config` push,
  `playfield annotations --clear`, `tags mount register`/`clear`)
  refuses `--all` up front, before any connection is attempted, with a
  message naming the restriction — never a silent single-target
  fallback. This refusal shape (`cmd_view`'s in `camera.py`) is the
  template every later single-target command in this package copies
  verbatim, `flatfield.py` included.
- **`camera tags --follow` is read-only but still refuses `--all`
  (sprint 039) — a documented, narrow exception to "fan-out is
  restricted to read-only commands," not a contradiction of it.** The
  fan-out table's premise is a bounded one-shot RPC per target,
  rendered and done; `--follow` is an unbounded blocking loop over one
  stream. `fan_out()`'s per-target "call, render, move to the next
  daemon" shape has no way to interleave N concurrently-blocking
  streams, so `--follow --all` is refused up front, before any
  connection, with a message pointing at running `--follow` once per
  daemon — the same refusal shape (checked client-side, before any
  RPC) every other `--all`-excluded command already uses, just for a
  different underlying reason (unboundedness, not write-shape).
- **`--json` on `camera tags --follow` is `cli/`'s first-ever
  machine-readable output mode (sprint 039) — a narrow, explicit
  exception to §6's "no `--json`/`--format` flag anywhere in this
  package," not a reversal of it.** Scoped to this one flag on this one
  command: every other command keeps printing human-aligned text only.
  The exception is justified because `--follow`'s entire purpose is
  recording a stream over time (the stakeholder's own use case), which
  a human-aligned table is a poor fit for once a script wants to
  consume it — `_format_tag_row`'s existing human-formatted string
  stays the default; `--json` swaps in one JSON object per line with
  the same fields, still going through the same `AprilCamError`
  ->`report_error` handling for a connection failure.
- **`camera flatfield`'s `--force`/`--dry-run` flags mirror `calibrate.py`'s
  opt-in-flag shape** (sprint 037): plain
  `store_true` flags, defaulted off, passed straight through as RPC
  request fields with no client-side interpretation — the daemon alone
  decides what they mean (which quality-gate checks `force` overrides;
  `dry_run` skips the persist/reload step). `--clear` is a fourth,
  mutually-exclusive-in-practice mode (checked client-side before any
  RPC: naming both `--clear` and a capture-only flag like `--exposure`
  is a `report_error`-shaped usage error, never a silent
  last-flag-wins).
- **Destination/precondition checks run before any RPC that would
  cost a resource.** `camera image`/`playfield image` check output
  writability and terminal-attachment *before* calling `get_frame` —
  "error before capture, not after" (UC-17) — so a doomed destination
  never costs a camera a frame it can't keep. `camera config`/
  `playfield config` validate the pushed document client-side
  (`ConfigLoader.load_*`) before any RPC, so "a bad file never reaches
  the daemon" (UC-23.S-4).
- **Camera/playfield resolution is client-side, deliberately, not a
  daemon shortcut.** `playfield image`/`view` take no camera
  positional; `_common.resolve_playfield_camera` reads
  `CameraState.playfield` links from a fresh `list_cameras()` call and
  resolves silently only when exactly one linked, present camera
  exists — zero or several is a reported ambiguity, never an arbitrary
  pick. The daemon's own RPCs remain camera-addressed, always; there
  is no daemon-side "the playfield's camera" convenience method
  (`daemon.interfaces.Daemon.get_frame`'s own docstring, per
  `playfield.py`'s module docstring).
- **Errors are typed, never string-matched.** Every `cmd_*` catches
  `aprilcam.errors.AprilCamError` and hands it to `_common.report_error`
  — never re-derives fatality or reconstructs a message from text,
  matching `errors.py`'s own stated rule that adapters dispatch on
  exception type, not message content.
- **Success/failure can be data, not an exception**, where a use case
  calls for it: `camera calibrate`'s `CalibrationResult` with
  `succeeded=False` is a normal RPC return value (missing ids or a
  named degenerate-geometry reason), distinct from a precondition or
  transport failure that *does* raise (`NoPlayfieldLinked`, unknown
  camera, connection failure).
- **Units convert at the CLI boundary and nowhere else.**
  `tags.py`'s `--yaw-deg` <-> `MountParameters.mount_yaw_rad` conversion
  happens only in `cli/tags.py`; the wire and every layer below it
  stay in radians. Each CLI module owns its own small argv-shaping
  helpers rather than sharing them across modules where the coupling
  isn't worth it — e.g. `_unwritable_reason` is duplicated verbatim in
  `camera.py` and `playfield.py` rather than factored into
  `_common.py`, the same "each side owns its own small conversion"
  posture `client/models.py` states for the proto boundary.
- **Empty results are success, not silence.** Every listing prints an
  explicit `(no cameras)` / `(no tags detected)` / `(no playfields)` /
  `(no annotations)` / `(no matches)` / `(no registered tags)` /
  `(no daemons found)` rather than nothing — a deliberate, repeated
  convention so an empty daemon reads as "genuinely empty," not as a
  command that silently produced no output.

## 4. Design

**Argument parsing conventions.** `-d`/`--daemon HOST` is a root
option, added once to the top-level parser via
`_common.add_daemon_option` (`aprilcam -d HOST camera list`), never to
a subcommand; omitted means `APRILCAM_DAEMON_HOST`, else the local
daemon. Commands that never talk to a daemon ignore it. `-h` is reserved for `--help` everywhere,
which is exactly why the daemon flag is `-d` and not something that
would collide. Fan-out-eligible commands additionally get `--all` via
`_common.add_all_option`; commands the fan-out table excludes simply
never get the flag, so attempting it is argparse's ordinary
unrecognised-argument error rather than a hand-rolled message.
Camera/playfield/tag identity arguments are positional; flags are used
for everything optional or modal (`--dump`, `--reset`, `--clear`,
`--force`, `--dry-run`, `--base64`).

**Output-format conventions.** Image-returning commands
(`camera image`, `playfield image`) support three destinations: a file
(`-o FILE`), base64 to stdout (`--base64`), or raw bytes to stdout
(the default, refused against an attached terminal unless `--force`).
`--all` combined with `camera image` is restricted to the base64
destination only — a single `-o FILE` can't hold more than one
daemon's frame, and concatenated raw binary from several daemons isn't
a valid image in any format — refused up front with a message naming
`--base64` as the alternative (`camera.py`'s
`_cmd_image_all`/`cmd_image`, a documented exception carved out of the
general "read-only commands fan out" rule, not an oversight). `--dump`
on `camera config`/`playfield config` prints the live document as
exact on-disk JSON (`json.dumps(..., indent=2, sort_keys=True)`) via
the corresponding `Get*` RPC — the read half of a push/read pair
sharing one positional argument and one noun, the same shape `--reset`
uses for `camera config`.

**Error -> exit-code mapping.** Two exit codes cover every `cmd_*`
outcome: `EXIT_OK = 0`, `EXIT_ERROR = 1` (`_common.py`). Argparse's own
parse failures use its standard code 2, untouched by this package.
`EXIT_PLACEHOLDER = 3` (sprint 047) is returned by `camera image` /
`playfield image` when the daemon served a NO SIGNAL test card
(`ImageFrame.placeholder`): the card is still written, a warning goes to
stderr. Viewers never exit on a placeholder; their status line says
"NO SIGNAL". `camera tags` prints "camera absent" (and `--json` carries
`camera_absent`) for absent-camera heartbeats.
Every typed `AprilCamError` maps to `EXIT_ERROR` via `report_error`,
which prints `f"error: {exc}"` to stderr — `str(exc)` is exactly the
message the error's own `__init__` already built (holder name,
unknown-camera-plus-available-list, etc.), so no adapter ever
re-derives or parses a message. `fan_out()` returns `EXIT_ERROR` if
*any* discovered daemon failed to answer, even though the successful
rows were still printed — so a script can detect a partial result —
and `EXIT_OK` for the trivial "found zero daemons" case, which is
success, not failure.

**The `fan_out` helper** (`_common.py`) is the one piece of real logic
in this package, because it's shared by nine different commands rather
than because it does anything domain-specific: it calls
`discovery.find_all()`, then, for each `DaemonInfo`, connects and
calls the caller-supplied `call(daemon)`, rendering successes via
`render(label, result)` and printing (not raising past) any individual
`ConnectionFailed`. A `DiscoveryFailed` from `find_all()` itself aborts
the whole command, since there's nothing left to iterate — distinct
from "enumerated zero," which is a normal empty result.

**Fan-out labels and HOST-N (sprint 048).** The label `fan_out` passes to
`render` is the daemon's short host name (`DaemonInfo.name`), with
` (local)` appended for the local daemon; it falls back to the address
only when a daemon advertises no name. `camera list --all` rows also show
a copyable `HOST-N` address (N = the bracketed persistent number). Every
camera-taking subcommand (`image`, `tags`, `view`, `close`, `config`,
`calibrate`, `flatfield`, `locate`, `intrinsics`) calls
`client.addressing` before connecting, so `donald-4` works without `-d`;
`donald-4` combined with a different `-d` is an error.

**`camera tags --follow`/`--json` (sprint 039).** `cmd_tags` gains a
`--follow` flag: instead of one `discovery.connect(args.daemon)
.get_tags(args.camera)` call, it opens `daemon.stream_tags(args.camera)`
(the client-side `TagStream` — `client.streams.SocketTagStream`,
already used by nothing in this package before this sprint) and, for
each yielded `TagFrame`, prints one `_format_tag_row`-shaped line per
tag (extended to include `image_yaw_deg` for the uncalibrated branch,
per `types.TagRecord`'s new field) prefixed with a timestamp, until a
`KeyboardInterrupt` (Ctrl-C) — at which point it closes the stream
(`TagStream.close()`, unsubscribing daemon-side on the next publish
tick per `client/streams.py`'s own contract) and returns `EXIT_OK`, the
same "Ctrl-C is a normal way to end a following command" convention
`camera view`'s window-close path already establishes, not a crash.
`--json` swaps `_format_tag_row`'s human line for one
`json.dumps({...})` line per tag with the same fields (`tag`,
`timestamp`, `pixel`, `image_yaw_deg`) — this package's first
machine-readable output (§3). No new client-library method is needed:
`GrpcDaemon.stream_tags` (`client/daemon_client.py`) already exists
from the streaming sprint; this ticket is purely `cli/camera.py`
consuming it.

**Rejected alternative, recorded in code:** `daemon.py`'s module
docstring records that `daemon status --all` was in-scope per
`cli.md`'s general fan-out table but was deliberately deferred out of
the sprint that first shipped fan-out, because there was no
`daemon list` yet to make multi-daemon status especially useful, and
that sprint's own discovery implementation couldn't find more than the
local daemon anyway — "recorded here as a scope decision, not an
oversight." `daemon list` (added later) needs no `-d`/`--all` flags at
all, unlike every other command's default-single/opt-in-fan-out shape,
because listing daemons is inherently "every daemon" — it reuses
`fan_out()` unmodified and needed no new RPC or `.proto` change.

**Rejected spelling, recorded in code:** `playfield tags` is the
eventual, documented command-surface spelling for listing detected
tags playfield-addressed (resolved client-side, mirroring
`playfield image`/`view`), but it depends on `CameraState.playfield`
links that don't exist until the config-service work lands. `camera
tags <camera>` ships instead, camera-addressed like `camera image`,
with the deferred spelling and the reasoning recorded directly in
`camera.py`'s module docstring rather than silently improvised.

### `camera intrinsics` (sprint 044)

`aprilcam [-d HOST] camera intrinsics <camera...> [--board SPEC] [--install] [--no-gui]` (`cli/intrinsics.py`). The board spec resolves request (`--board`) > env `APRILCAM_CHARUCO_BOARD` > default `5x7:37:28:DICT_5X5_100`. With a display it opens the fullscreen Tk GUI (`view/intrinsics_gui.py`, driven by the pure `view/intrinsics_model.py`); `--no-gui` (and any session with no display, e.g. over SSH) uses a text-mode status loop. The GUI never auto-quits on green; Enter installs the good cameras, Esc aborts. Exit code 0 only when every camera ended installed. `--install` installs good cameras on finish without the prompt. `camera list` rows gain `lens=<source>` and `lens-pending`; `camera calibrate` prints the lens model it carried forward.

## 5. Interfaces

### Exposes

The `aprilcam` command, one subcommand tree per noun:

```
aprilcam [-d HOST] <command> ...     # -d is a root option, before the command
aprilcam daemon   start|stop|restart|status|list
aprilcam camera   list|probe|image|tags|config|view|calibrate|flatfield|locate
aprilcam playfield list|config|image|view|annotations|where
aprilcam tags     mount register|clear|list | generate
aprilcam guide    [name]
aprilcam [-d HOST] mcp
```

Conventions shared across the whole surface (detailed in §4 and in
`docs/design/use-cases/cli.md`'s "Global conventions" section, not
repeated here): root-level `-d`/`--daemon HOST` targeting (omitted = local);
`--all` fan-out on read-only commands only; a camera is addressable by
persistent registry number or slug; `playfield`-addressed commands
that need a camera resolve it client-side rather than taking a
positional. Every subcommand returns `EXIT_OK`/`EXIT_ERROR` (or
argparse's own code 2 for a parse failure) and writes human-readable
text to stdout/stderr — there is no machine-readable output mode
(`--json`, etc.) anywhere in this package today.

### Consumes

- **`client/`** (`docs/design/design.md` §3) — the only real
  dependency, and the whole reason this package can stay thin:
  `client.discovery.Discovery` (`connect`, `find_all`),
  `client.lifecycle.DaemonLifecycle` (local-only process control),
  `client.guides.Guides`, and — **sprint 039** — `Daemon.stream_tags`
  (`client/daemon_client.py`, returning a `client.streams.SocketTagStream`)
  for `camera tags --follow`, this package's first consumer of a stream
  client rather than a one-shot RPC. Every RPC-shaped call in this
  package is one line into a `Daemon` handle `Discovery.connect()`
  returns.
- **`daemon.interfaces`** — not the daemon implementation (`cli/`
  never runs daemon code or touches cv2 through it), but the shared,
  cv2-free `Daemon`/`DaemonStatus`/`MountRegistration` interface and
  value types that both `client/` and this package type against.
- **`config/`** — `config.interfaces.ConfigLoader`/`Feature`/
  `CameraPosition`/`Directories` and `config.loader.ConfigLoader`, for
  client-side document loading/validation before a push RPC, and for
  `locate.py`'s on-disk `camera_position` write.
- **`view/`** (`docs/design/design.md` §3) — `view.viewer.run_raw_view`
  / `run_playfield_view`, the streaming windows `camera view`/
  `playfield view` hand a connected `Daemon` to; this package's own
  contribution is resolving the camera/daemon and reporting
  `viewer.NoDisplayAvailable`/`errors.NotCalibrated` with a
  command-specific pointer — and, **sprint 038**,
  `viewer.FirstFrameTimeout` with the same treatment.
- **`calibration.geometry`** — `locate.py` only, for
  `solve_camera_geometry`/`ReferenceObservation` (§3's documented
  exception).
- **`tags.generator.TagGenerator`** — `tags generate` only, the one
  offline, daemon-free capability in this package.
- **`errors.py`** — the typed `AprilCamError` taxonomy every `cmd_*`
  catches and hands to `report_error`.
- **`mcp.server.build_server`** — `mcp.py` only, to construct and run
  the MCP stdio server object.

## 6. Open Questions / Known Limitations

- **`camera flatfield`'s output format (spread-before/after reporting) has no machine-readable mode either** — same limitation as every other command in this package (below); an agent driving this workflow through the MCP tool instead of the CLI gets the structured `FlatFieldResult` directly.
- **No machine-readable output mode, except `camera tags --follow
  --json` (sprint 039).** Every other command still prints
  human-aligned text only; there is no general `--json`/`--format` flag
  across the package, so scripting against most of `cli/`'s output
  still means parsing the same text a human reads (`fan_out`'s exit
  code remains the one thing designed for script consumption on those
  commands). Whether to generalize `--json` to other commands (`camera
  tags` one-shot, `camera list`, ...) is an open call for a future
  sprint, not decided here — this sprint's scope is narrowly the
  streaming-recording use case `--follow` serves.
- **`cli/` is still not enforced cv2-free**, unlike `client/`, `view/`,
  and `mcp/` (`tests/test_no_cv2_in_client_modules.py`'s
  `_CV2_FREE_PACKAGES`) — even though, as of sprint 036, no file in this
  package transitively imports `cv2` today (`locate.py`'s former leak via
  `calibration/geometry.py` is closed; see §3). Nothing prevents a future
  change from reintroducing one, since this package is not in the AST
  scan's enforced set. Whether to add `cli/` to `_CV2_FREE_PACKAGES` now
  that it is actually clean is an open call for a future sprint, not
  decided here.
- **`_unwritable_reason` is duplicated** between `camera.py` and
  `playfield.py` rather than shared via `_common.py` — a deliberate
  choice per each module's docstring, but it means the two copies can
  drift if one is edited without the other.
- **No structured logging or `--verbose`/`--quiet` flags.** All
  output is the one fixed verbosity level baked into each `cmd_*`.
- **`playfield tags`, the documented eventual spelling for
  playfield-addressed tag listing, does not exist yet** — `camera tags
  <camera>` is the shipped stand-in (§4's "rejected spelling," which
  is really "deferred spelling"). Adding it later means retiring or
  aliasing the camera-addressed form, an open design question this
  package's code does not resolve.
- **`playfield annotations` is list/clear only** — authoring
  (`put`/`draw`) is Python-client-library-only by explicit scope
  decision; there is no CLI path to author an annotation today.

**`camera close <camera> [--force]` (sprint 047 ticket 007).** Releases the
device via `CloseCamera`; refused while in use unless `--force` (exit 1 with
the `CameraInUse` message). `camera list` shows a deliberately closed camera
as `closed` (distinct from `absent`). MCP mirrors it as `close_camera`.
