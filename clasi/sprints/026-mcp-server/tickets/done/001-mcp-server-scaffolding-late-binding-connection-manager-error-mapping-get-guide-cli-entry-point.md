---
id: '001'
title: MCP server scaffolding, late-binding connection manager, error mapping, get_guide,
  CLI entry point
status: done
use-cases:
- SUC-001
- SUC-005
depends-on: []
github-issue: ''
issue: mcp-server-the-agent-facing-product.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# MCP server scaffolding, late-binding connection manager, error mapping, get_guide, CLI entry point

## Description

Foundation ticket for the sprint. Every later ticket (002-006) registers
tools against the shapes this ticket establishes, so this one is
serially first and nothing else in the sprint can start before it.

Build the two cross-cutting concerns the issue and sprint architecture
call out — late-binding connection resolution (UC-22) and typed-error
mapping, analogous to `daemon/rpc.py`'s `_STATUS_CODES` table — plus the
image-output helper, server assembly, the `get_guide` tool (the one
tool that must work with **no** daemon anywhere, UC-27.S-1), and the
`aprilcam mcp` CLI entry point.

**Resolve the SDK error-surfacing question concretely here** (per
sprint.md's Architecture Open Questions and the stakeholder-accepted
plan): read the installed `mcp>=1.0` package's actual tool-error
contract and implement `mcp/errors.py` against it — raise the SDK's
tool-error type if that produces a clean `isError: true` result to the
caller without crashing the transport; fall back to a documented
`{"error": ...}` sentinel (v1's pattern) only if raising does not work
cleanly. Whichever is chosen, every later ticket's tools use it
uniformly via this module — document the decision in this module's
docstring, since it fixes the tool-registration shape for the rest of
the sprint.

No daemon-side change. No cv2 anywhere in `mcp/`.

## Acceptance Criteria

- [x] `src/aprilcam/mcp/__init__.py` created — package docstring states
      the adapter discipline (`adapters.md`: translation layer only, no
      authoritative state, no vision) the same way `client/interfaces.py`
      and `daemon/interfaces.py` state theirs.
- [x] `mcp/connection.py`: a `ConnectionManager` (plain class, not
      `typing.Protocol`) that resolves a `Daemon` via
      `client.interfaces.Discovery`, caches one per resolved target key,
      and evicts+reconnects **on the next call** after a
      `ConnectionFailed` from an actual RPC — never a preflight probe,
      never a reconnect-per-call. Constructor takes an optional
      `default_host` (the CLI's `-d` value) and an injectable
      `discovery_factory` for testability. Never performs a daemon RPC
      at construction time.
- [x] `mcp/errors.py`: one decorator/wrapper (used by every tool
      function in tickets 002-005) implementing exactly the
      error-mapping table in `sprint.md`'s Architecture Design
      Rationale (every `aprilcam.errors.AprilCamError` subtype passed
      through as `str(exc)`; any other exception becomes a generic
      internal-error tool failure, logged server-side with its full
      traceback, never echoed to the agent). Implements the concrete
      SDK mechanism decided above.
- [x] `mcp/images.py`: a helper taking already-encoded bytes + encoding
      + requested `format` (`"base64"` | `"file"`) and returning the
      matching MCP content — `"base64"` inline, `"file"` written into a
      per-server-process temp directory
      (`tempfile.mkdtemp(prefix="aprilcam-mcp-")`, created once at
      server startup) with only the path returned. No decode, no
      re-encode, no cv2 — the bytes from `Daemon.get_frame` pass
      through unchanged.
- [x] `mcp/server.py`: `build_server(*, default_daemon_host=None,
      connection_factory=..., ...) -> FastMCP` — assembles a
      `FastMCP("aprilcam", instructions=...)` instance with orientation
      instructions (the golden path: `get_guide` first, then
      perception/configuration), and registers the `get_guide` tool.
      `get_guide` reads via `client.guides.Guides()` **directly** — no
      `ConnectionManager`, no `Discovery`, no `Daemon` call at all —
      mirroring `cli/guide.py`'s documented exception to the rest of
      the package (UC-27.S-1: must work before any daemon exists).
- [x] `cli/mcp.py`: `aprilcam mcp [-d HOST]` using
      `cli/_common.py`'s `add_daemon_option` (mirrors every other
      `cli/` module's flag), builds the server with that host as the
      connection manager's default, and runs it over stdio. No daemon
      contacted during argument parsing or server construction.
- [x] `cli/__init__.py`: imports and wires `mcp.add_subparser` alongside
      `daemon`, `camera`, `playfield`, `tags`, `guide`.
- [x] `tests/test_no_cv2_in_client_modules.py`: `_CV2_FREE_PACKAGES`
      extended to include `"mcp"` — this gate must exist before any
      later ticket's tool modules are added, since they all land under
      the same scan.
- [x] Zero `typing.Protocol` anywhere in `mcp/` — every interface here
      is a plain base class (`raise NotImplementedError`) or a frozen
      `@dataclass(kw_only=True)`, per `daemon/interfaces.py`'s module
      docstring rule.
- [x] `uv run mypy` clean.
- [x] Full suite green (baseline 976 tests passing, headless + hardware,
      plus everything this ticket adds).

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/cli/test_main.py
  tests/test_no_cv2_in_client_modules.py tests/test_typing.py -v`, plus
  the full headless suite to confirm zero regressions from the new
  `cli/__init__.py` wiring.
- **New tests to write**: connection manager — resolves and caches per
  target, evicts on `ConnectionFailed` and reconnects on the next call,
  never touches a daemon at construction (fake `Discovery`); error
  mapping — one test per `aprilcam.errors` subtype plus one unexpected-
  exception case, asserting the exact SDK-level error shape chosen
  above; image helper — `base64` returns inline content, `file` writes
  under the session temp dir and returns only the path; `get_guide` —
  proven to work against a `Discovery`/`Daemon` stub that raises if
  ever called, positively proving zero daemon contact (UC-27.S-1); CLI
  — `-d`/`-h` argument parsing, mirroring `tests/aprilcam/cli/
  test_daemon_cli.py`'s style.
- **Verification command**: `uv run pytest -m "not camera"` (CI), full
  `uv run pytest` plus `uv run mypy` on this development machine.

## Implementation Plan

**Approach**: build the two cross-cutting concerns (`connection.py`,
`errors.py`) and the image helper first, since every later ticket's
tool functions are written against their exact shape; then server
assembly plus `get_guide` as the first real tool, proving the pipe
works end-to-end with no daemon required; CLI last, since it is a thin
shell over `build_server`.

**Files to create**: `src/aprilcam/mcp/__init__.py`,
`src/aprilcam/mcp/connection.py`, `src/aprilcam/mcp/errors.py`,
`src/aprilcam/mcp/images.py`, `src/aprilcam/mcp/server.py`,
`src/aprilcam/cli/mcp.py`.

**Files to modify**: `src/aprilcam/cli/__init__.py`,
`tests/test_no_cv2_in_client_modules.py`.

**Documentation updates**: none beyond docstrings this ticket —
`AGENT_GUIDE.md`'s user-facing update is ticket 006's job, once the
full tool surface exists to describe accurately.
