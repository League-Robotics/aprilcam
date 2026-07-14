---
id: '001'
title: Add resolve_code_or_none and narrow resolve_camera_code's exception handling
status: done
use-cases:
- SUC-003
depends-on: []
github-issue: ''
issue: aprilcam-view-cannot-parse-host-qualified-camera-codes.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Add resolve_code_or_none and narrow resolve_camera_code's exception handling

## Description

Foundation ticket for Sprint 017. Adds a non-raising wrapper around the
existing (and already-correct) `resolve_code()` in `client/host_codes.py`,
then uses it to narrow the overly broad exception handling in
`cli/_daemon.py:resolve_camera_code`. This does not change any behavior —
`resolve_code`/`code_for` are unchanged — it only removes a `except
(ValueError, Exception)` code smell (the `Exception` clause is redundant
with `ValueError` and silently swallows unrelated bugs) ahead of tickets
002-004, which build on `resolve_camera_code` as the sole camera-code
resolver.

See `architecture-update.md` §1 ("`client/host_codes.py` — becomes the
single canonical format+parse module") and §2 ("`cli/_daemon.py` — remove
the divergent letter+digit parser") for full design context.

## Acceptance Criteria

- [x] `client/host_codes.py` has a new function `resolve_code_or_none(code,
      store)` that calls `resolve_code(code, store)` and returns `None`
      instead of raising when `resolve_code` raises `ValueError` (malformed
      code, unknown host, unknown camera). No changes to `code_for`,
      `resolve_code`, `resolve_host_token`, `num_to_alpha`, or
      `alpha_to_num`.
- [x] `cli/_daemon.py:resolve_camera_code` calls `resolve_code_or_none`
      instead of its own `try: resolve_code(...) except (ValueError,
      Exception): return None` block. The bare `Exception` catch is
      removed — only the specific failure mode (invalid/unmatched code)
      is swallowed; unrelated exceptions propagate.
- [x] `resolve_camera_code`'s external contract is unchanged: still returns
      `(resolved_host, cam_enum)` on a code match or `None` otherwise, and
      still patches `args.daemon_host` for remote hosts.
- [x] Existing `tests/test_host_codes.py` coverage for `code_for`,
      `resolve_code`, and `resolve_host_token` remains green, unmodified.
- [x] New unit tests for `resolve_code_or_none` mirror the existing
      `resolve_code` test cases (local single-letter match, remote
      two-letter match, unknown host, unknown camera, malformed code) plus
      the "returns `None`, does not raise" assertion for each failure case.

## Implementation Plan

**Approach**: Additive change in `host_codes.py` (new function only), then
a narrowing edit in `_daemon.py` (replace exception clause, no signature
change).

**Files to create/modify**:
- `src/aprilcam/client/host_codes.py` — add `resolve_code_or_none(code:
  str, store: dict) -> tuple[dict, dict] | None`, docstring modeled on the
  existing `resolve_code` docstring, delegating entirely to `resolve_code`.
- `src/aprilcam/cli/_daemon.py` — in `resolve_camera_code` (around lines
  158-169), replace `from aprilcam.client.host_codes import load_store,
  resolve_code` + `try: ... resolve_code(...) except (ValueError,
  Exception): return None` with `from aprilcam.client.host_codes import
  load_store, resolve_code_or_none` and a direct call, handling the `None`
  return the same way the current code handles the caught exception.
- `tests/test_host_codes.py` — add a `TestResolveCodeOrNone` class (or
  extend the existing `resolve_code` test class) with the mirrored cases.

**Testing plan**:
- Run `uv run pytest tests/test_host_codes.py -v` to confirm new and
  existing tests pass.
- Run the full suite (`uv run pytest`) to confirm no regression elsewhere
  (`resolve_camera_code` is also exercised indirectly via any existing
  `view_cli`/`tags_cli` tests, if present).

**Documentation updates**: None required — this is an internal
implementation-detail change with no user-visible behavior change and no
public CLI surface change.
