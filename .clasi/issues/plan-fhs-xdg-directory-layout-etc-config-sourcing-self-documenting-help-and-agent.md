---
status: pending
---

# Plan: FHS/XDG directory layout, /etc config sourcing, self-documenting `--help`, and `--agent`

## Context

We are turning AprilCam into a **system daemon** (likely running as root, or a
dedicated `aprilcam` user) on Raspberry Pis. The current layout conflates three
different kinds of state under two ad-hoc directories:

- `data_dir` (default `./data/aprilcam/`, **cwd-relative**) holds **persistent**
  state — `cameras/registry.json` (stable camera identity + enumeration),
  `cameras/<slug>/calibration.json`, the developer-owned `config.json`,
  `paths.json`, and `playfields/*.json` — **and** the daemon log `aprilcamd.log`.
- `socket_dir` (default `/tmp/aprilcam/`) holds **ephemeral** runtime —
  `control.sock`, stream `*.sock`, `aprilcamd.pid`, `aprilcamd.spawn.lock`.

`/var/run` (= `/run`) is **tmpfs, wiped on every reboot**, so it must hold only
the ephemeral runtime, not persistent calibration/registry. This plan moves to an
**FHS-correct layout** (with XDG fallbacks for non-root dev), sources a
system-wide `/etc/aprilcam.env`, makes `--help` document configuration, and adds
`aprilcam --agent` to emit complete AI-agent instructions.

Naming uses the existing convention **`aprilcam`** (no hyphen) throughout —
`/etc/aprilcam.env`, `/var/lib/aprilcam`, `/run/aprilcam`.

### Grounding (from exploration)

- `Config.load()` chain (`config.py:299-382`): `~/.aprilcam` < `.aprilcam` <
  `.env` < `APRILCAM_*` env. **No `/etc` sourcing today.** Relative paths are
  `.resolve()`'d to absolute at load (`_path()` helper, `:329`). `socket_dir` is
  `mkdir`'d at load (`:380`); `data_dir` is created lazily by writers.
- Persistent vs ephemeral split confirmed: `registry.json`
  (`camera/registry.py:214`), `calibration.json` (`calibration/calibration.py:471`),
  `config.json` (`camera/camera_config.py:52`, "written exclusively by the MCP
  server / operators"), `paths.json` (`mcp_server.py:305`), playfields
  (`core/playfield_def.py`) are PERSISTENT; `*.sock`/pid/lock are EPHEMERAL;
  `aprilcamd.log` is a LOG currently under `data_dir`
  (`client/control.py:160`, `daemon/client.py:105`).
- CLI dispatch (`cli/__init__.py`): matches `args[0]`; `_print_help()` (`:71`);
  existing `aprilcam config` subcommand (`cli/config_cli.py`) shows resolved
  config; `--version` exists. There is **already** a `config` subcommand to lean on.
- Agent guides live at `src/aprilcam/AGENT_GUIDE.md` + `ROBOT_API_GUIDE.md`
  (package-data), read via `(_PACKAGE_DIR / "AGENT_GUIDE.md").read_text()`
  (`mcp_server.py:4323-4359`). `AGENT_GUIDE.md` is the full AI-agent instruction
  set. Reuse this reader.
- Existing env-var doc table: `docs/wiki/daemon-interface.md:272-277`
  (4 core vars); extended vars documented as `config.py` field comments.

---

## Recommended directory layout

Separate the three concerns by purpose; FHS for system/root, XDG for user/dev;
everything overridable by `APRILCAM_*` env + the dotfile chain.

| Concern | System default (`euid==0`) | Dev default (non-root) | Env override |
|---|---|---|---|
| **Config** | `/etc/aprilcam/aprilcam.env` (+ legacy `/etc/aprilcam.env`) | `$XDG_CONFIG_HOME/aprilcam` (`~/.config/aprilcam`) | dotfile chain |
| **Data (persistent)** | `/var/lib/aprilcam/` | `$XDG_DATA_HOME/aprilcam` (`~/.local/share/aprilcam`) | `APRILCAM_DATA_DIR` |
| **Runtime (ephemeral)** | `/run/aprilcam/` | `$XDG_RUNTIME_DIR/aprilcam` (`/run/user/<uid>/aprilcam`) | `APRILCAM_SOCKET_DIR` |
| **Logs** | journald (systemd) or `/var/log/aprilcam/` | `$XDG_STATE_HOME/aprilcam` (`~/.local/state/aprilcam`) | `APRILCAM_LOG_DIR` (new) |

`cameras/` and `playfields/` live under the **data** dir (persistent);
`control.sock`/streams/`aprilcamd.pid`/`spawn.lock` under **runtime**;
`aprilcamd.log` under **logs** (split out of `data_dir`). Default selection is
automatic by `euid` (override with `APRILCAM_SYSTEM=1`/`0`); paths still
`.resolve()` to absolute.

---

## Changes

### 1. Config sources — add `/etc` (`config.py` `Config.load`)
Insert as the **lowest** precedence, before `~/.aprilcam`: parse
`/etc/aprilcam.env`, then `/etc/aprilcam/aprilcam.env` (both, system-wide), via
the existing `_parse_dotfile`. New precedence (highest wins):
`APRILCAM_*` env > `.env` > `.aprilcam` > `~/.aprilcam` > `/etc/aprilcam(.env)`.
Update the `Config` docstring precedence list (`config.py:236-240`).

### 2. Directory defaults — FHS/XDG (`config.py`)
- Add `_default_dirs()` returning `(config_dir, data_dir, runtime_dir, log_dir)`
  chosen by `euid`/`APRILCAM_SYSTEM` (FHS) vs XDG env (dev).
- `data_dir` default ← data_dir; `socket_dir` default ← runtime_dir;
  `daemon_pidfile` default ← runtime_dir/`aprilcamd.pid`.
- **New `log_dir` field + `APRILCAM_LOG_DIR`**; route `aprilcamd.log` there —
  update `client/control.py:160` and `daemon/client.py:105` to use
  `config.log_dir` instead of `config.data_dir`.
- Create `data_dir`/`log_dir` at load (like `socket_dir` already is), but **guard
  `/run` & `/var/lib` permission errors** with a clear message (rely on systemd
  `*Directory=` or pre-created dirs when not root).

### 3. `--help` documents configuration + env vars (`cli/__init__.py` `_print_help`)
- Add a **Configuration** section: the source-precedence chain and the resolved
  dirs (or "run `aprilcam config` to see resolved paths").
- Add a **table of every `APRILCAM_*` variable** with default + one-line
  description.
- **Single source of truth:** define the variable list/descriptions once as
  `CONFIG_VARS` in `config.py`, reused by `_print_help()`, the existing
  `aprilcam config` (`config_cli.py`), and to regenerate
  `docs/wiki/daemon-interface.md` + `.env.example`. Include the static-deskew
  vars and the `APRILCAM_DAEMON_HOST/PORT` from the remote-daemon issue.

### 4. `--agent` prints AI-agent instructions (`cli/__init__.py` `main`)
- Add a top-level `--agent` flag (listed in help) that prints `AGENT_GUIDE.md` to
  stdout; `--agent robot` prints `ROBOT_API_GUIDE.md`.
- Factor a shared `read_guide(name)` helper (small module) reused by the MCP
  server's `get_agent_guide`/`get_robot_api_guide`/resources so both read the
  same packaged files (DRY).
- Refresh `AGENT_GUIDE.md` to mention the new config/dir model and that
  `aprilcam --agent` exists.

### 5. Docs & deploy sync
- Update `.env`, `.env.example`, `.aprilcam`, `docs/wiki/daemon-interface.md`:
  new defaults, `/etc` sourcing, `APRILCAM_LOG_DIR`, the layout table.
- **systemd unit** (`deploy/aprilcamd.service`, also referenced by the
  remote-daemon issue): use `ConfigurationDirectory=aprilcam` (→`/etc/aprilcam`),
  `StateDirectory=aprilcam` (→`/var/lib/aprilcam`),
  `RuntimeDirectory=aprilcam` (→`/run/aprilcam`),
  `LogsDirectory=aprilcam` (→`/var/log/aprilcam`), `StandardOutput=journal`.
  systemd creates these with correct ownership, so the explicit
  `APRILCAM_DATA_DIR`/`APRILCAM_SOCKET_DIR` env lines become unnecessary when the
  FHS defaults are used. Prefer a dedicated `aprilcam` system user (or
  `DynamicUser=`) over root; root also works.

---

## Files to change

- `src/aprilcam/config.py` — `/etc` sources, FHS/XDG `_default_dirs()`, `log_dir`
  + `APRILCAM_LOG_DIR`, `CONFIG_VARS` table, updated docstring.
- `src/aprilcam/cli/__init__.py` — expanded `_print_help()`, `--agent` flag.
- `src/aprilcam/cli/config_cli.py` — reuse `CONFIG_VARS`; show the resolved layout.
- `src/aprilcam/client/control.py`, `src/aprilcam/daemon/client.py` — log to
  `log_dir`.
- `src/aprilcam/AGENT_GUIDE.md` (+ new shared `read_guide` helper reused by
  `server/mcp_server.py`).
- Docs: `.env`, `.env.example`, `.aprilcam`, `docs/wiki/daemon-interface.md`.
- Deploy: `deploy/aprilcamd.service` (systemd `*Directory=` directives).

## Verification

- `aprilcam --help` shows the config chain + the `APRILCAM_*` table; `aprilcam
  config` shows resolved dirs matching the layout — run as user → XDG paths,
  `sudo aprilcam config` (euid 0) → `/etc/aprilcam`, `/var/lib/aprilcam`,
  `/run/aprilcam`, `/var/log/aprilcam`.
- `aprilcam --agent` prints `AGENT_GUIDE.md` in full; `--agent robot` prints
  `ROBOT_API_GUIDE.md`.
- Drop `APRILCAM_DATA_DIR=…` into `/etc/aprilcam.env` → confirm it's honored
  (lowest precedence) and that an `APRILCAM_*` env var still overrides it.
- Persistence: restart the daemon → `/var/lib/aprilcam` keeps
  calibration/registry; reboot (or wipe `/run`) → `/run/aprilcam` is recreated,
  persistent state intact.
- Unit tests: config precedence incl. `/etc`; `euid`/XDG-based default selection
  (monkeypatch `os.geteuid` + XDG env); `log_dir` resolution; `CONFIG_VARS`
  coverage (every `APRILCAM_*` field is documented).
- Run `pytest`; bump version (`[[feedback_bump_version]]`,
  `[[feedback_version_scheme]]`).

## Process / relationship to the remote-daemon issue

CLASI project — this plan will be filed as a **pending issue** on approval (like
the remote-daemon plan). It pairs with that issue and should land **first**, so
the remote-daemon systemd unit and deploy runbook adopt the new `*Directory=`
directives and FHS defaults instead of the ad-hoc `/home/eric/aprilcam-data`.
