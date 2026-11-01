---
id: '001'
title: DaemonInfo.name and host-name fan-out labels
status: done
use-cases:
- SUC-001
depends-on: []
github-issue: ''
issue: host-qualified-camera-names-donald-4.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# DaemonInfo.name and host-name fan-out labels

## Description

`find_all()` receives each daemon's TXT `host` name (`_BrowseResult.hostname`)
but drops it. Carry it through `DaemonInfo.name` and label fan-out blocks with
it instead of `IP:port` / `localhost`. The local daemon is labelled with this
machine's short hostname plus ` (local)`.

## Implementation Plan

- `client/interfaces.py`: add `name: str = ""` to the frozen, kw_only
  `DaemonInfo` dataclass (a field, never a property; no Protocol).
- `client/discovery.py`: add `short_host_name(raw) -> str` (lowercase, strip
  `.local` and any domain suffix; `Donald.local` -> `donald`; an IP-looking
  value is returned unchanged). Populate `name` for browsed entries from
  `_BrowseResult.hostname` and for the local entry from `socket.gethostname()`.
  Do NOT change `DaemonInfo.host` (its `ip:port` format is load-bearing, see
  the module docstring) or the self-filter. Add `short_host_name` to `__all__`.
- `cli/_common.py` `fan_out()`: label = `info.name`, plus ` (local)` for the
  local daemon; fall back to the old address label when `name` is empty. The
  unreachable-daemon stderr line uses the same label.

## Acceptance Criteria

- [x] `DaemonInfo.name` exists with default `""`; existing constructions still work
- [x] `find_all()` fills `name` normalized for browsed and local entries
- [x] `Donald.local`, `DONALD`, `donald.home` all normalize to `donald`
- [x] `fan_out` headers read `gala (local):`, `buzzkill:`, `donald:`
- [x] Empty `name` falls back to the previous address label
- [x] `host` still `ip:port` for browsed entries

## Testing

- **Existing tests to run**: `tests/aprilcam/client`, `tests/aprilcam/cli/test_common.py`, `tests/aprilcam/cli/test_camera_cli.py`
- **New tests to write**: `short_host_name` table test; `find_all()` with the injected `browse` seam returning a hostname (assert `name`); `fan_out` label tests with a fake discovery (local, named remote, nameless remote, unreachable remote)
- **Verification command**: `uv run pytest tests/aprilcam/client tests/aprilcam/cli`
