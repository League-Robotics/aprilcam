---
id: '003'
title: Camera address resolver (client/addressing.py)
status: done
use-cases:
- SUC-002
- SUC-003
depends-on:
- '001'
github-issue: ''
issue: host-qualified-camera-names-donald-4.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Camera address resolver (client/addressing.py)

## Description

One shared, adapter-neutral resolver that turns a camera argument plus the
effective daemon target into `(host, camera)`. Used by both the CLI (004) and
the MCP `ConnectionManager` (005). Pure logic over `Discovery` and `Daemon`;
no CLI or MCP imports. `N` in `HOST-N` is the persistent registry number.

## Implementation Plan

Create `src/aprilcam/client/addressing.py` implementing the sprint.md
"Resolution order" exactly:

1. Argument not matching `^(.+)-(\d+)$`: pass through, no RPC.
2. Else `list_cameras()` on the target daemon; exact slug or number match
   wins. Target unreachable (`ConnectionFailed`): pass through unchanged.
3. Else split at the last `-`; match `short_host_name(HOST)` against
   `find_all()` names, case-insensitively; local hit gives `host=None`.
4. No name hit: direct connect to `HOST` validated by `list_cameras()` (connect
   may be lazy); on failure raise `UnknownHost` listing daemon names found.
5. Explicit target that differs from the resolved daemon (by name or address)
   raises `ConflictingTarget`; same daemon is accepted.

Return a small frozen `ResolvedCamera(host: Optional[str], camera: str)`
dataclass (fields, no Protocol). Add `UnknownHost` and `ConflictingTarget`
(`AprilCamError` subclasses) to `errors.py`, with messages that name the
daemons found. `DiscoveryFailed` from `find_all()` propagates. Resolve the
open questions recorded in sprint.md (lazy connect; `-d IP` conflict check
when the daemon is not discoverable) and note the decision in the module
docstring.

## Acceptance Criteria

- [x] `donald-4` with local target resolves to Donald's daemon and camera `4`
- [x] `Donald-4` and `donald-4` behave identically; `donald.local` style names normalize
- [x] `brio-501` and `hd-pro-webcam-c920` stay as slugs when the target daemon lists them (no host lookup)
- [x] A number-only or hyphenless argument causes no RPC
- [x] `donald-4` with explicit `-d buzzkill` raises `ConflictingTarget`; with `-d donald` or Donald's address it is accepted
- [x] Unknown host raises `UnknownHost` whose message lists the daemons found
- [x] Unreachable target daemon: argument passes through unchanged
- [x] Module imports nothing from `aprilcam.cli` or `aprilcam.mcp`, and no cv2

## Testing

- **Existing tests to run**: `tests/aprilcam/client`, `tests/test_no_cv2_in_client_modules.py`
- **New tests to write**: `tests/aprilcam/client/test_addressing.py` with a fake `Discovery` (find_all/connect) and fake `Daemon`s; one test per acceptance bullet plus a daemon literally named `brio`
- **Verification command**: `uv run pytest tests/aprilcam/client tests/test_no_cv2_in_client_modules.py`
