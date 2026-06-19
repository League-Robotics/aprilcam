---
status: draft
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 013 Use Cases

## SUC-001: System operator installs AprilCam as a root service and gets correct FHS directories

- **Actor**: System operator (root or via sudo) on a Raspberry Pi or Linux host.
- **Preconditions**: AprilCam installed system-wide. No `APRILCAM_*` env vars set.
- **Main Flow**:
  1. Operator starts the daemon (directly or via systemd unit).
  2. `Config.load()` calls `_default_dirs()`; detects euid == 0 → selects FHS
     directories: data → `/var/lib/aprilcam/`, runtime → `/run/aprilcam/`,
     log → `/var/log/aprilcam/`.
  3. Daemon log is written to `/var/log/aprilcam/aprilcamd.log`.
  4. Calibration and registry files persist in `/var/lib/aprilcam/` across reboots.
  5. After a reboot, systemd recreates `/run/aprilcam/` via `RuntimeDirectory=`
     and the daemon starts without stale sockets/pidfiles.
  6. Operator runs `sudo aprilcam config` and sees FHS paths.
- **Postconditions**: Daemon is running with correct FHS directories. No manual
  env overrides needed for a standard install.
- **Acceptance Criteria**:
  - [ ] `Config.load()` as root returns data_dir `/var/lib/aprilcam`, socket_dir
        `/run/aprilcam`, log_dir `/var/log/aprilcam`.
  - [ ] `aprilcamd.log` is written under `log_dir`, not `data_dir`.
  - [ ] `APRILCAM_SYSTEM=1` forces FHS paths even for non-root.
  - [ ] `sudo aprilcam config` shows FHS paths.

---

## SUC-002: Developer runs AprilCam as a non-root user and gets XDG directories

- **Actor**: Developer working on a robotics project.
- **Preconditions**: AprilCam installed in user space (pipx). No `APRILCAM_*` env vars set.
- **Main Flow**:
  1. Developer runs `aprilcam daemon start` as a regular user.
  2. `Config.load()` calls `_default_dirs()`; detects euid != 0 → selects XDG
     directories: data → `~/.local/share/aprilcam/`, runtime →
     `$XDG_RUNTIME_DIR/aprilcam`, log → `~/.local/state/aprilcam/`.
  3. Calibration and registry files persist in the developer's home directory.
  4. Developer runs `aprilcam config` without sudo and sees XDG paths.
- **Postconditions**: AprilCam state is isolated per-user under XDG directories.
  No root access required.
- **Acceptance Criteria**:
  - [ ] `Config.load()` as non-root returns XDG-derived data_dir, socket_dir,
        log_dir.
  - [ ] `$XDG_DATA_HOME`, `$XDG_RUNTIME_DIR`, `$XDG_STATE_HOME` are respected
        when set.
  - [ ] Fallbacks apply when XDG vars are unset
        (`~/.local/share`, `/run/user/<uid>`, `~/.local/state`).
  - [ ] `APRILCAM_SYSTEM=0` forces XDG paths even for root.

---

## SUC-003: Operator sets system-wide configuration in `/etc/aprilcam.env`

- **Actor**: System operator.
- **Preconditions**: AprilCam installed as a system service. Operator wants to set
  defaults for all instances.
- **Main Flow**:
  1. Operator creates `/etc/aprilcam.env` with `APRILCAM_DETECTION_FPS=30`.
  2. All AprilCam processes started subsequently pick up this value at the lowest
     precedence (source 0, before `~/.aprilcam`).
  3. A developer with `APRILCAM_DETECTION_FPS=10` in `~/.aprilcam` overrides the
     system default for their processes.
  4. `/etc/aprilcam/aprilcam.env` is also checked (alternative location); both
     files are optional; absence is not an error.
- **Postconditions**: System-wide defaults are managed via `/etc`. Per-user and
  per-project dotfiles still override them.
- **Acceptance Criteria**:
  - [ ] A value in `/etc/aprilcam.env` is loaded when no higher-priority source
        overrides it.
  - [ ] `/etc/aprilcam/aprilcam.env` is also checked and loaded with the same
        semantics.
  - [ ] A matching key in `~/.aprilcam` overrides the `/etc` value.
  - [ ] A process-level `APRILCAM_*` env var overrides all dotfile sources.
  - [ ] Missing `/etc/aprilcam.env` does not raise an error.

---

## SUC-004: Developer runs `aprilcam --help` and learns all configuration variables

- **Actor**: Developer or operator.
- **Preconditions**: AprilCam is installed. No daemon running required.
- **Main Flow**:
  1. Developer runs `aprilcam --help`.
  2. The output includes a "Configuration" section showing the full source-precedence
     chain from `/etc/aprilcam.env` up to process env, and a pointer to
     `aprilcam config` for resolved paths.
  3. The output includes a table of all `APRILCAM_*` variables, each with its
     default value and a one-line description.
  4. The table is generated from `CONFIG_VARS` defined in `config.py`.
- **Postconditions**: Developer can configure AprilCam without reading source code.
- **Acceptance Criteria**:
  - [ ] `aprilcam --help` output includes a "Configuration" section.
  - [ ] All `APRILCAM_*` variables defined in `Config` appear in the table.
  - [ ] Each entry has a non-empty default and description.
  - [ ] `CONFIG_VARS` in `config.py` is the single source; `_print_help()` and
        `config_cli.py` both import it.
  - [ ] Static-deskew variables (`APRILCAM_STATIC_DESKEW`, `APRILCAM_DESKEW_PX_PER_CM`,
        `APRILCAM_UNDISTORT`, `APRILCAM_MOVEMENT_THRESHOLD_PX`) are included.

---

## SUC-005: AI agent or developer retrieves full agent instructions from the CLI

- **Actor**: AI agent or developer preparing to use AprilCam MCP tools.
- **Preconditions**: AprilCam installed. No MCP session required.
- **Main Flow**:
  1. User runs `aprilcam --agent` → `AGENT_GUIDE.md` is printed to stdout and
     exits 0.
  2. User runs `aprilcam --agent robot` → `ROBOT_API_GUIDE.md` is printed to
     stdout and exits 0.
  3. Both commands use `read_guide(name)` from `src/aprilcam/guides.py`, which
     resolves the package data path.
  4. The MCP server's `get_robot_api_guide` tool and `aprilcam://docs/agent-guide`
     resource also call `read_guide()` so content is identical.
  5. `aprilcam --help` lists `--agent` in the flags section.
  6. `AGENT_GUIDE.md` is refreshed to mention the new config/dir model and that
     `aprilcam --agent` exists.
- **Postconditions**: Agent instructions accessible from shell, MCP, or CI; content
  is consistent across all paths.
- **Acceptance Criteria**:
  - [ ] `aprilcam --agent` exits 0 and prints non-empty content.
  - [ ] `aprilcam --agent robot` exits 0 and prints non-empty content.
  - [ ] `aprilcam --agent unknown` prints an error and exits non-zero.
  - [ ] `--agent` appears in `aprilcam --help` output.
  - [ ] MCP `get_robot_api_guide` result equals `aprilcam --agent robot` output
        (same file read by `read_guide()`).
  - [ ] `AGENT_GUIDE.md` mentions the FHS/XDG directory model.
