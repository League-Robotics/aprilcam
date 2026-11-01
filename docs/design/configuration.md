# Configuration and Directories

**Status:** draft 2026-07-30 · **Owner:** Eric Busboom
**Related:** [use-cases/daemon.md](use-cases/daemon.md) UC-01 · [specification.md](specification.md) §5

Where AprilCam's files live, and what overrides what. The goal is a
cascade a person can hold in their head — v1's six-level FHS/XDG
resolution across four directories was more machinery than the problem
needs.

---

## 1. Two directories, not four

The split that matters is **who writes it**:

| | Default | Override | Written by |
|---|---|---|---|
| **Config** | `/etc/aprilcam` | `APRILCAM_CONFIG_DIR` | a human (or `scp`) |
| **State** | `/var/lib/aprilcam` | `APRILCAM_STATE_DIR` | the daemon |

`/etc` is admin-owned and **not writable by the daemon**. That is the
point of it: a human edits config there, and the daemon only reads it. A
daemon writing calibration data into `/etc` would be wrong, so derived
state gets its own root under `/var/lib`.

```
/etc/aprilcam/                      # human-owned, daemon reads only
  playfields/<slug>.json            # playfield definitions
  cameras/<slug>/config.json        # camera configs

/var/lib/aprilcam/                  # daemon-owned, regenerable
  cameras/registry.json             # identity -> stable enum number
  cameras/<slug>/calibration.json   # homography, static markers
  cameras/<slug>/info.json          # advisory flags
```

Everything under `/var/lib` is **regenerable**: delete it and the system
rebuilds it by re-probing and re-calibrating. Nothing there is precious,
and nobody hand-edits it. Everything under `/etc` is the opposite —
hand-authored, version-controlled in a repo, and never written by the
software.

### Sockets and logs are not configurable

- **Sockets** → `/run/aprilcam` (or `$XDG_RUNTIME_DIR/aprilcam` when not
  running as a system service). Runtime plumbing; nobody needs to choose
  where it lives.
- **Logs** → stderr, which means journald under systemd and the terminal
  when run by hand.

Both were configurable in v1 (`APRILCAM_SOCKET_DIR`, `APRILCAM_LOG_DIR`)
and neither needed to be.

## 2. Unprivileged fallback

A developer running `aprilcam daemon` on a laptop cannot write
`/var/lib/aprilcam` and may not be able to read a `/etc/aprilcam` that
does not exist. When the system directories are unavailable, both roots
fall back to the user's home:

| | System | User fallback |
|---|---|---|
| Config | `/etc/aprilcam` | `~/.config/aprilcam` |
| State | `/var/lib/aprilcam` | `~/.local/share/aprilcam` |

The fallback is per-directory and automatic. **The daemon reports which
roots it resolved at startup**, so there is never a mystery about which
config was loaded — the single most common way a cascade wastes an
afternoon.

## 3. The full cascade

For any setting, in precedence order:

1. **Command-line flag** — e.g. `-d` / `--daemon` for daemon selection.
2. **Environment variable** — `APRILCAM_*`.
3. **Configuration file** in the config directory.
4. **Built-in default.**

Three levels above the default, and a flag always wins. Specifically,
`-d pi-field.local` beats `APRILCAM_DAEMON_HOST=other.local`, which
beats anything in a file — the ordinary Unix expectation, and previously
undefined.

## 4. Environment variables

The whole set. If a variable is not here, it does not exist.

| Variable | Effect |
|---|---|
| `APRILCAM_CONFIG_DIR` | Override the config root |
| `APRILCAM_STATE_DIR` | Override the state root |
| `APRILCAM_DAEMON_HOST` | Default daemon to connect to (overridden by `-d`) |
| `APRILCAM_DAEMON_PORT` | Default daemon port |
| `APRILCAM_LOG_LEVEL` | Daemon log level |
| `APRILCAM_IDLE_CLOSE_S` | Seconds a camera may sit unused (no stream, tag loop, intrinsics session or read) before the daemon releases its device; `0` disables; default `60` |

v1 had roughly fourteen. The ones dropped were tuning knobs that belong
in a camera or playfield config document rather than the environment —
`APRILCAM_DETECTION_FPS`, `APRILCAM_STATIC_DESKEW`,
`APRILCAM_DESKEW_PX_PER_CM`, `APRILCAM_UNDISTORT`,
`APRILCAM_MOVEMENT_THRESHOLD_PX` — plus the socket/log/pidfile paths and
the FHS/XDG switch, all now handled by §1 and §2.

**Rule going forward:** an environment variable is for *where things are*
and *what to talk to*. Anything describing how a camera or playfield
behaves belongs in that camera's or playfield's document, where it is
version-controlled and pushable.

## 5. Relationship to the ephemeral-config rule

[use-cases/daemon.md](use-cases/daemon.md) UC-01 establishes that
configuration **pushed over RPC is in-memory only** and discarded on
restart, while **files are the source of record**. This document says
where those files are; it does not change that rule.

The full picture, restated once:

| | Written to a file | Survives restart |
|---|---|---|
| Config in `/etc/aprilcam` | by a human | **yes** |
| Config pushed over RPC | no | no |
| Calibration in `/var/lib/aprilcam` | by the daemon | **yes** |
| Tag mount parameters (UC-12) | no | no |
| Annotations (UC-28) | no | no |

## 6. Open questions

- **Does a config push ever write to `/etc`?** No — and it cannot, since
  the daemon does not have write access there by design. Making a pushed
  config permanent is `aprilcam camera config --dump` plus a file copy
  ([use-cases/cli.md](use-cases/cli.md) UC-24). This is a *consequence*
  of the read-only `/etc` decision, and a good one: it makes the
  ephemeral rule structural rather than a policy the daemon has to
  remember.
- **Is `APRILCAM_DIR` worth having** as a single override that sets both
  roots under one parent (`$APRILCAM_DIR/etc`, `$APRILCAM_DIR/var`)? It
  would make a self-contained test or demo install a one-liner. Adds a
  fourth thing to the cascade, though.
- **Migration from v1 layouts** — v1 had `migrate-dirs`. Is there
  anything to migrate, or does v2 start clean?
