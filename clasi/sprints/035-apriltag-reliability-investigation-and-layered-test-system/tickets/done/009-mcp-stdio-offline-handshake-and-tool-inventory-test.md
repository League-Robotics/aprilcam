---
id: 009
title: MCP stdio offline handshake and tool-inventory test
status: done
use-cases:
- UC-22
depends-on: []
github-issue: ''
issue: aprilcam-v2-layered-test-system.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# MCP stdio offline handshake and tool-inventory test

## Description

Starts ticket-group 3 (L1). Spawns a REAL `aprilcam mcp` subprocess over
stdio (via the `mcp` SDK's `mcp.client.stdio`, with the v1 hand-rolled
JSON-RPC client design as a documented fallback if `asyncio_mode=auto`
bridging proves awkward — see
`git show 65510e4^:tests/system/test_mcp_subprocess.py` in the
AprilTags repo), completes the MCP handshake, and asserts `tools/list`
is a superset of the pinned 27-tool inventory. Also verifies the server
does not eagerly connect to a daemon at startup (per `mcp/DESIGN.md`'s
"zero daemon RPCs during construction" invariant) — if it does, that
finding gets this test marked `integration` instead of left unmarked, so
the gate's assumptions stay honest. This ticket has no camera or fixture
dependency — it can run with no daemon and no hardware at all.

## Acceptance Criteria

- [x] `tests/aprilcam/mcp/test_stdio_offline.py` spawns
      `aprilcam mcp` as a real subprocess, completes the MCP
      initialize handshake, and calls `tools/list`.
- [x] The returned tool set is a superset of a pinned, explicit 27-tool
      list (named in the test, not derived from `mcp/DESIGN.md`'s prose,
      so a change to either is caught).
- [x] No daemon is started or connected to during this test — verified
      (e.g., by asserting no daemon socket/process exists before and
      after, or via an injected connection-attempt counter) rather than
      merely assumed from the source code's stated invariant.
- [x] If step above finds an eager connect, the test is marked
      `@pytest.mark.integration` and the finding is recorded in the
      ticket's implementation notes — this ticket does not silently
      "fix" `mcp/server.py` to make the assumption true; that would be
      out of this ticket's scope (report via the exception protocol if
      it contradicts `mcp/DESIGN.md`'s documented invariant).
- [x] The subprocess is cleanly terminated at test teardown in every
      case (pass, fail, or error) — no orphaned `aprilcam mcp` process
      survives the test run.

## Implementation Notes (verification pass, 2026-08-23)

The test module already existed (committed in `dba1b45` from an earlier
interrupted attempt) and was re-verified against every acceptance
criterion rather than rewritten, since it held up:

- **No eager daemon connect**: confirmed both empirically (the test's
  `socket_path.exists()` assertions at three checkpoints -- before the
  subprocess starts, mid-session after `initialize`+`tools/list`, and
  after full teardown -- all pass) and by reading `mcp/connection.py`
  and `mcp/server.py`: `ConnectionManager.__init__` only stores its
  arguments, and `ConnectionManager.resolve()`/`Discovery.connect()`
  are invoked exclusively from `ConnectionManager.call()`, which only
  `mcp/tools/*.register`-registered tool *functions* invoke when
  actually called -- never from `build_server`'s registration pass,
  never from MCP's `initialize`, never from `tools/list` (which only
  enumerates already-registered tool schemas). So criterion 4's
  condition (an eager connect found) never triggers; the test correctly
  stays unmarked (no `integration` marker) and `mcp/server.py` was not
  touched.
- **27-tool pin accuracy**: cross-checked `_EXPECTED_TOOLS` against
  every real `@mcp.tool()` decorator in `server.py` (1),
  `tools/perception.py` (7), `tools/configuration.py` (8),
  `tools/mobile_tags.py` (3), and `tools/annotations.py` (8) = 27,
  excluding one docstring mention of the literal string `@mcp.tool()`
  in each tools module (module-docstring prose, not a real decorator).
  Exact match, and the test's assertion (`missing = _EXPECTED_TOOLS -
  tool_names`) is genuinely a superset check, not equality.
- **Clean subprocess teardown**: confirmed by reading the installed
  `mcp` SDK's `mcp/client/stdio/__init__.py` -- its `stdio_client`
  async context manager's `finally` block closes stdin, waits briefly,
  then escalates to `_terminate_process_tree` (SIGTERM, then SIGKILL on
  timeout) unconditionally, so it runs on the exception path too, not
  just on a clean return.
- **Real subprocess, not skipped**: re-ran the test; it found and
  exercised the real `.venv/bin/aprilcam` console script (editable
  install) rather than hitting the `pytest.skip` fallback.

No code changes were required. Re-ran
`uv run pytest tests/aprilcam/mcp/test_stdio_offline.py -v`: 2 passed
in 3.78s, no orphaned `aprilcam mcp` process before or after (checked
via `ps aux | grep 'aprilcam mcp'`).

## Implementation Plan

**Approach**: Prefer the `mcp` SDK's own `mcp.client.stdio` client
first, since it is the maintained, spec-compliant option; fall back to a
minimal hand-rolled JSON-RPC-over-stdio client (mirroring v1's design,
cited above) only if `asyncio_mode=auto` bridging proves genuinely
awkward — record which path was taken and why in the test module's
docstring either way.

**Files to create**:
- `tests/aprilcam/mcp/__init__.py` (new package)
- `tests/aprilcam/mcp/test_stdio_offline.py`

**Testing plan**:
- No hardware, no daemon needed — pure subprocess + stdio protocol
  test.
- **Verification command**: `uv run pytest tests/aprilcam/mcp/test_stdio_offline.py -v`

**Documentation updates**:
- None required; traceability for this test is filled alongside the
  rest of L1 (see ticket 018's final sweep, or update inline if
  finished early).
