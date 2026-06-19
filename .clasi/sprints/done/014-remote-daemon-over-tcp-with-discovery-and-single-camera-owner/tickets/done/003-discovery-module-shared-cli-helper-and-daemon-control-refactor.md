---
id: 014-003
title: client/discovery.py, shared cli/_daemon.py, and DaemonControl no-spawn refactor
status: done
use-cases:
- SUC-001
- SUC-003
depends-on:
- 014-001
---

# 014-003: client/discovery.py, shared cli/_daemon.py, and DaemonControl no-spawn refactor

## Description

Three closely related changes that together give every client a consistent,
auto-discovering path to the daemon:

1. **`client/discovery.py`** — mDNS `ServiceBrowser` and `resolve_daemon_target()`.
2. **`cli/_daemon.py`** — shared argparse group (`--daemon-host`/`--daemon-port`)
   and `connect_from_args()` helper used by all CLI commands.
3. **`DaemonControl.connect_default` refactor** — remove the `subprocess.Popen`
   auto-spawn block; call `resolve_daemon_target()` instead.
4. **`Config` fields** — activate (or add) `daemon_host`/`daemon_port` and
   confirm `APRILCAM_DAEMON_HOST`/`APRILCAM_DAEMON_PORT` entries in `CONFIG_VARS`.
5. **`zeroconf` to base deps** in `pyproject.toml`.
6. **`aprilcam mcp` argv parsing** — `mcp_server.py:main()` parses
   `--daemon-host`/`--daemon-port`.

## Acceptance Criteria

- [x] `src/aprilcam/client/discovery.py` exists with:
  - `DaemonInfo` dataclass: `name`, `host`, `port`, `addresses`.
  - `discover_daemons(timeout=1.0) -> list[DaemonInfo]` using zeroconf
    `ServiceBrowser` on `_aprilcam._tcp.local.`. Returns `[]` if `zeroconf`
    import fails.
  - `resolve_daemon_target(config, cli_args=None) -> tuple[str, int, str|None]`
    returning `(host, port, unix_path_or_None)` with precedence:
    1. `cli_args.daemon_host` / `cli_args.daemon_port`
    2. `config.daemon_host` / `APRILCAM_DAEMON_HOST`
    3. Local unix socket probe (never spawn)
    4. mDNS browse: 1 → auto; >1 → error with list; 0 → hard error
- [x] `src/aprilcam/cli/_daemon.py` exists with:
  - `add_daemon_args(parser)` — adds `--daemon-host`/`--daemon-port` argparse group.
  - `connect_from_args(config, args) -> DaemonControl` — calls
    `resolve_daemon_target` and returns a connected `DaemonControl`.
- [x] `client/control.py` `connect_default()` no longer contains a
    `subprocess.Popen` call. On failure to connect, it raises
    `DaemonNotFoundError` (new exception in `aprilcam/errors.py`) with message:
    "no aprilcam daemon found — start one (`systemctl start aprilcamd` /
    `aprilcam daemon start`) or set `APRILCAM_DAEMON_HOST`."
- [x] `DaemonNotFoundError` is a subclass of `RuntimeError` in `errors.py`.
- [x] `config.py` has `daemon_host: str | None = None` and `daemon_port: int = 5280`
    fields, read from `APRILCAM_DAEMON_HOST` and `APRILCAM_DAEMON_PORT`.
- [x] `CONFIG_VARS` in `config.py` has entries for `APRILCAM_DAEMON_HOST` and
    `APRILCAM_DAEMON_PORT` (activate stubs from Sprint 013 if present, or add now).
- [x] `pyproject.toml` base `dependencies` includes `"zeroconf>=0.131"`. It is
    removed from the `daemon` extra.
- [x] `mcp_server.py:main()` parses `--daemon-host`/`--daemon-port` from argv
    and passes them (or stores them on the config) before starting the MCP server.
- [x] All CLI commands that connect to the daemon (`cameras`, `tags`, `calibrate`,
    `view`, `web`, `daemon`) call `add_daemon_args` and `connect_from_args`.
    (For commands not yet updated in this ticket, at minimum add the args group;
    refactor the connection call in the relevant workstream tickets.)
- [x] Unit tests in `tests/test_discovery.py`:
  - Mock `discover_daemons` to return 0, 1, and >1 results; assert
    `resolve_daemon_target` behavior matches the precedence spec.
  - Assert `DaemonControl.connect_default` raises `DaemonNotFoundError`
    (not spawns) when the daemon is unreachable.
  - Assert `APRILCAM_DAEMON_HOST` env var bypasses mDNS.
- [x] `uv run pytest` passes.

## Implementation Plan

### Approach

Write `client/discovery.py` first (no deps on other tickets). Then write
`cli/_daemon.py`. Then refactor `DaemonControl.connect_default`. Then update
`config.py` and `pyproject.toml`. Finally wire the argv parsing in `mcp_server.py`
and add the argparse group to CLI commands.

### Files to Create/Modify

- `src/aprilcam/client/discovery.py` — new module.
- `src/aprilcam/cli/_daemon.py` — new module.
- `src/aprilcam/client/control.py` — remove spawn block; call
  `resolve_daemon_target`; import `DaemonNotFoundError`.
- `src/aprilcam/errors.py` — add `DaemonNotFoundError`.
- `src/aprilcam/config.py` — add `daemon_host`, `daemon_port`; update
  `CONFIG_VARS`.
- `pyproject.toml` — move `zeroconf>=0.131` from `daemon` extra to base deps.
- `src/aprilcam/server/mcp_server.py` — `main()` parses daemon argv; stores
  resolved host/port for use by `_ensure_daemon_client()`.
- `src/aprilcam/cli/cameras_cli.py`, `calibrate_cli.py`, `tags_cli.py`,
  `view_cli.py`, `web_cli.py`, `daemon_cli.py` — add `add_daemon_args` call.
- `tests/test_discovery.py` — new test module.

### Discovery module implementation notes

```python
# client/discovery.py (sketch)
from __future__ import annotations
import time
from dataclasses import dataclass, field

@dataclass
class DaemonInfo:
    name: str
    host: str
    port: int
    addresses: list[str] = field(default_factory=list)

def discover_daemons(timeout: float = 1.0) -> list[DaemonInfo]:
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError:
        return []
    results: list[DaemonInfo] = []
    # ... ServiceBrowser on _aprilcam._tcp.local., collect add_service callbacks
    # sleep(timeout), close zeroconf, return results
    ...

def resolve_daemon_target(config, cli_args=None):
    # Priority 1: explicit CLI flag
    if cli_args and getattr(cli_args, "daemon_host", None):
        return (cli_args.daemon_host, cli_args.daemon_port or config.daemon_port, None)
    # Priority 2: env / config
    if config.daemon_host:
        return (config.daemon_host, config.daemon_port, None)
    # Priority 3: local unix socket probe
    unix = str(config.socket_dir / "control.sock")
    if _probe_unix(unix):
        return ("localhost", config.daemon_port, unix)
    # Priority 4: mDNS
    found = discover_daemons()
    if len(found) == 1:
        return (found[0].host, found[0].port, None)
    if len(found) > 1:
        names = ", ".join(f.host for f in found)
        raise DaemonNotFoundError(
            f"Multiple aprilcam daemons found ({names}). "
            f"Set APRILCAM_DAEMON_HOST to select one."
        )
    raise DaemonNotFoundError(
        "No aprilcam daemon found — start one "
        "(`systemctl start aprilcamd` / `aprilcam daemon start`) "
        "or set APRILCAM_DAEMON_HOST."
    )
```

### Testing Plan

- Unit tests mock `discover_daemons` and `_probe_unix`; test all four
  precedence branches.
- Integration test: start daemon locally, confirm `connect_default` connects
  without spawning.
- Confirm `APRILCAM_DAEMON_HOST=bogus.local connect_default` raises
  `DaemonNotFoundError` (not spawn).

### Documentation Updates

- Update `AGENT_GUIDE.md` to document that the daemon must be running before
  using the MCP server (no auto-spawn).
- Add a note to `deploy/README.md` (created in ticket 009).
