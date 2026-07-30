# CLI

**Owner:** Eric Busboom · **Last reviewed:** 2026-07-29 · **Status:** in-flux

---

## 1. Purpose

The `aprilcam` command: a single dispatcher (`cli/__init__.py`) plus one thin module per subcommand. The seam is *human entry points* — everything a person types lives here, and each module is expected to be either a pure daemon client (via `client/`) or a local utility, never a camera opener or vision engine. The subsystem alone owns argv conventions: global `--host`/`--port` handling, the base-vs-`[daemon]` install boundary (actionable install hints instead of tracebacks), and the shared daemon-connection helpers (`_daemon.py`) every subcommand reuses.

## 2. Orientation

`main()` in `cli/__init__.py` handles `--help`/`--version`/`--agent`, strips global daemon flags, then lazily imports the target module from the `SUBCOMMANDS` dict and calls its `main(remaining)`. Subcommands and their jobs:

- `daemon` — start/status/stop the daemon (the one command that runs vision).
- `mcp` — run the MCP stdio server (dispatches to `aprilcam.server.mcp_server`).
- `view` — tkinter live viewer fed by daemon streams (`view_cli.py`, 1,185 lines).
- `calibrate` — playfield calibration for one or more cameras (currently runs the cv2 math client-side).
- `cameras` — list cameras via daemon enumerate.
- `tags` — one-shot detect/list of all markers on a camera.
- `probe` — discover daemons via mDNS and update the `hosts.json` code store.
- `mobile` — manage the daemon's mobile-tag registry.
- `config` — print version and resolved configuration.
- `init` — write MCP server entries for Claude Code / VS Code.
- `taggen` — generate AprilTag/ArUco images or PDFs (offline; needs cv2).
- `tool` — list/inspect/run MCP tools from the command line.
- `migrate-dirs` — move legacy co-located config files into `config_dir`.
- `web` — start the HTTP/WS server (removed in v2; currently broken, see §6).

`_daemon.py` supplies `add_daemon_args` (adds `--host`/`--port` with `--daemon-host`/`--daemon-port` aliases, dest `daemon_host`/`daemon_port`), `connect_from_args` (resolve → connect → probe `ListCameras`), and `resolve_camera_code` (alpha codes like `A`/`FB` → host + camera).

## 3. Constraints and Invariants

- **Dispatch is lazy** — subcommand modules import only when named, so a base (cv2-free) install can run `cameras`/`tags`/`view`/`mcp` without tripping over daemon-only dependencies. Adding a module-level heavy import to `cli/__init__.py` breaks the base install for every command.
- **`DAEMON_COMMANDS = {daemon, taggen, calibrate}`** is the exact set that gets the "install `aprilcam[daemon]`" hint on `ModuleNotFoundError`; every other command re-raises. Adding a cv2 dependency to a command without updating this set gives users raw tracebacks (this is live today for `view`, see §6).
- **Global `--host`/`--port` are extracted from *any* argv position** (`_extract_global_flags`, both `--flag VALUE` and `--flag=VALUE`, all four alias spellings) **and re-exported as `APRILCAM_DAEMON_HOST` / `APRILCAM_DAEMON_PORT` env vars** before dispatch. Downstream commands pick them up through the normal `Config` precedence — no plumbing of a namespace through every module. Trap: any subcommand wanting its *own* `--host`/`--port` semantics (e.g. a bind address) never sees the flag; the root dispatcher eats it as a daemon target.
- **CLI commands never open cameras or probe local hardware** — enumeration and frames come from the daemon (`aprilcam calibrate` enumerates via RPC and reads frames through a `CaptureFrame` adapter). The daemon's `CameraPipeline` is the only camera owner.
- **User-facing camera numbers are the persistent enumeration numbers** (what `aprilcam cameras` prints), resolved to live OS indices via daemon enumerate / `resolve_enum_to_index` — never the volatile OS index.
- **Alpha camera codes resolve by slug first, stored enum second** (`resolve_camera_code` contract) — stored enums go stale across replug; slug is the reliable key.
- **`--host` accepts a single-letter host code** — `connect_from_args` runs `resolve_host_token` on the *final* resolved host, so `A`/`F` works from the flag, the env var, or config alike.

## 4. Design

**Dispatcher.** Deliberately not argparse-with-subparsers: a dict of `{name: {help, module}}` and string dispatch keeps startup at import-nothing speed and lets `--help` render from static metadata. The env-var re-export for global flags is the load-bearing design decision — it decouples the root parser from thirteen subcommand parsers at the cost of the `web` bind-flag collision noted above.

**Daemon connection.** All subcommands share `connect_from_args`: `resolve_daemon_target` (client-owned four-level precedence) → host-code resolution → `DaemonControl.connect()` → probe. Failure surfaces as `DaemonNotFoundError` with start-the-daemon guidance; nothing in `cli/` ever spawns the daemon implicitly.

**`calibrate_cli.py`.** Loads the playfield definition registry from the *local* config root (`config_playfields_dir`), enumerates cameras via the daemon, warms up capture (retrying `UNAVAILABLE: no frame captured yet`), wraps the daemon in `_DaemonCapture` (a minimal `cv2.VideoCapture` mimic over `capture_frame`), and runs `calibrate_from_playfield_def` locally — writing `calibration.json` into the local `cameras_dir` and then `reload_calibration` on the daemon. This works only when the CLI runs on the daemon host (shared filesystem); it is the CLI half of the vision-boundary violation the v2 `Calibrate` RPC removes. A `--joint` two-camera mode calibrates a secondary camera from the primary's homography.

**`view_cli.py`.** A self-contained tkinter application: connects via `connect_from_args` (with legacy `--unix-path`/`--tcp-port` back-compat), resolves the camera (alpha code, enum, or pattern), opens it via RPC, then runs two reader threads (image stream, tag stream) feeding queues drained by a tk `_poll` loop. Rendering delegates deskew/overlay drawing to `ui.PlayfieldDisplay`, seeded per-frame from the *stream's* published `playfield_corners`/`homography`/origin — the viewer holds no calibration of its own. Collapsible panels (`CollapsibleFrame`) lazily enable object detection and the paths panel; paths are read from the camera's local `paths.json` (daemon-written via `SetPaths`), another shared-filesystem assumption.

## 5. Interfaces

### Exposes

- The `aprilcam` executable and `python -m aprilcam`: subcommands listed in §2; global flags `--host HOST` (hostname, IP, or host-code letter), `--port PORT`, `--agent [NAME]`, `-V/--version`. Exit codes: subcommand `main()` return value (0 default), 1 on unknown command or missing-dependency hint.
- `cli._daemon.add_daemon_args(parser)` / `connect_from_args(config, args)` / `resolve_camera_code(camera_arg, config, args) -> (host, enum, slug) | None` — the shared connection surface for any new subcommand (and used by `server/mcp_server.main`).
- `SUBCOMMANDS` / `DAEMON_COMMANDS` — the registration point for new commands and their install-hint classification.

### Consumes

- `client/` — `DaemonControl`, `resolve_daemon_target`, host_codes store, stream consumers (view). Owning: client.
- `server/mcp_server`, `server/web_server` — the `mcp` and `web` subcommands' targets. Owning: server.
- `ui/PlayfieldDisplay` — viewer rendering (pulls in cv2). Owning: ui.
- `calibration/` (`calibrate_from_playfield_def`, `calibrate_single`, `calibrate_secondary`) and `camera/` (registry, identity, `camutil.select_camera_by_pattern`, `camera_config`) — calibrate/cameras paths. Owning: calibration, camera.
- `config.Config`, `guides.read_guide`. Owning: top-level.

## 6. Open Questions / Known Limitations

- **BUG — `calibrate --joint` raises `NameError`**: the joint path references `field_width`/`field_height` (calibrate_cli.py ~lines 287–312) which are never assigned anywhere in `main()` — the variables died when dimensions moved into the playfield definition. The single-camera path is unaffected. Fix or delete `--joint`; v2 makes the whole command a pure RPC orchestrator (spec §4.4).
- **BUG — `aprilcam web` crashes at startup**: `web_cli.py` registers its own `--port`/`--host` (bind address) and then calls `add_daemon_args`, which registers `--host`/`--port` again → `argparse.ArgumentError: conflicting option string: --host` before any request is served (verified by execution). Additionally, its banner prints an `/mcp/sse` URL no route implements, and `create_app()` is called with no arguments, so daemon-connection flags would be dropped even if parsing succeeded. **v2 removes `web` entirely** (spec §4.9, §6) — delete rather than fix.
- **`view` is not actually cv2-free**: `cli/__init__.py`'s comment classifies it as an OpenCV-free thin client, but `view_cli.main` imports `aprilcam.ui.display`, which imports cv2 at module level — on a base install this is a raw `ModuleNotFoundError` (view is not in `DAEMON_COMMANDS`). Resolved in v2: the daemon publishes the deskewed stream and the viewer's cv2 dependency disappears (spec §4.8).
- **`calibrate` reads playfield definitions and writes calibration from/to local disk**, contradicting the daemon-as-rendezvous principle for remote daemons; v2 moves execution into the daemon behind a `Calibrate` RPC (spec §4.4).
- **v2 layout changes**: `view_cli.py` + the `ui/` draw helpers become the `view/` subsystem (spec §4.8); `web` is deleted; the remaining commands stay thin daemon clients under `cli/` (spec §4.9); `homocal`-era leftovers referenced from calibration go with spec §6.
