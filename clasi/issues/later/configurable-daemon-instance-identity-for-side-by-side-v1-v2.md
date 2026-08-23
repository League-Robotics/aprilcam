---
status: pending
---

# Configurable daemon instance identity, so v1 and v2 can run side by side

## Description

Two aprilcam daemons cannot currently coexist on one host. Every piece
of the daemon's identity is hardcoded, so a second daemon — a second v2,
or the v1 daemon a robot still depends on — collides with the first and
refuses to start:

```
aprilcam.errors.AlreadyRunning: a daemon is already running: pid 25028
```

The motivating case: **a robot needs to keep reaching the cameras
through v1 while v2 is under development on the same machine.**

## The collision surface

It is not only the socket name. Five things are fixed:

1. **`socket_dir`** — `_resolve_socket_dir()` in
   `src/aprilcam/config/directories.py`: `/run/aprilcam` →
   `$XDG_RUNTIME_DIR/aprilcam` → `<tmpdir>/aprilcam-<uid>`. Deliberately
   **excluded** from the flag/env cascade that `config_dir` and
   `state_dir` get — configuration.md §1 reasons that "sockets are
   runtime plumbing nobody needs to choose the location of". This issue
   is a request to revisit that decision, so the design doc needs
   updating alongside the code.
2. **`control.sock` and `daemon.lock`** — `SOCKET_FILENAME` /
   `LOCK_FILENAME` in `src/aprilcam/daemon/lifecycle.py:91`. The lock is
   what enforces one-daemon-per-`socket_dir` and produces the
   `AlreadyRunning` above.
3. **Per-camera stream sockets** — `stream-<name>.sock`,
   `src/aprilcam/daemon/stream_transport.py:145`.
4. **TCP control port** — `DEFAULT_DAEMON_PORT = 5280` in
   `config/directories.py`, described in spec §4.1 as *the* fixed
   control-plane port.
5. **mDNS advertisement** — service type `_aprilcam._tcp.local.`
   (`daemon/mdns.py`, `client/discovery.py:145`). Two instances would
   advertise the same type, and discovery fans out to anything found on
   `DEFAULT_DAEMON_PORT`.

## v1 collides on exactly the same paths

v1 is not accidentally out of the way:

- `archive/src/aprilcam/daemon/__main__.py:112` —
  `unix_path = args.unix_path or str(config.socket_dir / "control.sock")`
- `archive/src/aprilcam/daemon/client.py:134` —
  `control_path = config.socket_dir / "control.sock"`
- `archive/src/aprilcam/config.py:213` — same
  `/run/aprilcam` · `$XDG_RUNTIME_DIR/aprilcam` story
- plus a global lock, default `/tmp/aprilcamd.lock`
  (`archive/src/aprilcam/errors.py:52`)

Same filename, same directory family. Worth noting the irony: **v1 was
more configurable here than v2.** v1 exposed a `--unix-path` flag and a
settable `socket_dir`; v2 removed both.

## The plumbing already exists — it just has no surface

`DaemonLifecycle` accepts `socket_path` and `lock_path` overrides
(`lifecycle.py:194-230`) and `daemon/__main__.py:126` accepts a
`socket_path`. Both are used only by tests. There is no CLI flag and no
environment variable: `aprilcam daemon start` takes nothing but
`-d/--daemon`, and `python -m aprilcam.daemon` does not parse `--help`
at all. Exposing what is already there is most of the work.

## Proposal

A single instance knob that namespaces everything, plus escape hatches:

- **`APRILCAM_INSTANCE=<name>`** (and `--instance`) deriving, by default:
  - `socket_dir` → `<resolved socket_dir>/<name>` (or the socket/lock
    filenames prefixed — either works, one must be picked)
  - TCP port → an explicit per-instance port; **do not** auto-offset from
    5280, since a silent offset is undiscoverable by a client
  - mDNS instance name → distinct, so `aprilcam daemon list` can show
    both and address them separately
- **Individual overrides** for the cases the single knob does not cover:
  `APRILCAM_SOCKET_DIR`, `APRILCAM_DAEMON_PORT`. `socket_dir` should
  join the existing flag > env > default > fallback cascade rather than
  getting a bespoke mechanism.
- `config_dir` / `state_dir` already have env overrides, so a second
  instance can be given its own roots today. That should be documented
  as part of this: two instances sharing one `state_dir` would fight over
  `cameras/registry.json` and each other's `calibration.json`.

## The part this does not solve

**Instance isolation fixes addressing, not device contention.** A UVC
camera can generally be opened by one process at a time. This was hit
directly on 2026-08-12: with v1 MCP servers running out of `AprilTags/`
and `radio-robot-elite/`, the v2 daemon reported `no frame available
from camera 'hd-usb-camera'` while `camera probe` said
`in use by a live subscriber`; only restarting the v2 daemon cleared it.

So for the motivating case — robot on v1, development on v2 — the two
daemons need **disjoint camera sets**, and nothing today lets you say
which cameras a daemon may claim. That suggests a companion capability:
a per-instance camera allow/deny list (e.g. `cameras: [...]` in config,
or `APRILCAM_CAMERAS=slug,slug`), so v2 can be told to touch only
`hd-usb-camera` while v1 keeps the arducam serving the robot.

Without that, a second instance starts cleanly and then silently
contends for hardware — which is a worse failure than refusing to start,
because it looks like it worked.

Split this second half into its own issue if it is large enough to
warrant separate scheduling; it is recorded here because instance
isolation without it does not actually deliver the motivating use case.
