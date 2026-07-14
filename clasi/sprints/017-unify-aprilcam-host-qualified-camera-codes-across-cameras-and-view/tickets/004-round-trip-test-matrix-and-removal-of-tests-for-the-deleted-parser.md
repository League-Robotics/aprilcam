---
id: '004'
title: Round-trip test matrix and removal of tests for the deleted parser
status: in-progress
use-cases:
- SUC-001
- SUC-002
- SUC-003
depends-on:
- '002'
- '003'
github-issue: ''
issue: aprilcam-view-cannot-parse-host-qualified-camera-codes.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Round-trip test matrix and removal of tests for the deleted parser

## Description

Closing ticket for Sprint 017. Adds the round-trip regression coverage the
issue explicitly asked for: every code the `cameras` listing logic can
emit (via `code_for`, now used consistently by both listing paths per
ticket 003) must parse back through `resolve_code` (the sole parser after
ticket 002) to the exact `(host, camera)` pair it was generated from,
across a matrix of store shapes. This is the regression guard that
prevents the two-scheme drift that caused the original bug from
recurring.

Note: `TestResolveHostLetterNumber` (testing the deleted
`resolve_host_letter_number`) was already removed in ticket 002, to keep
the tree green at every ticket boundary — this ticket does not need to
remove it again, only verify it is gone and add the new coverage.

See `architecture-update.md` §7 ("Tests") and the Test Strategy section of
`sprint.md` for full detail.

## Acceptance Criteria

- [ ] A round-trip test matrix exists (new test module or a new test class
      in `tests/test_host_codes.py`) covering at minimum:
  - [ ] Local-only store (one local host, multiple cameras, no remotes).
  - [ ] Single-remote store (local + one remote host, multiple cameras on
        each).
  - [ ] Multi-remote store (local + 2+ remote hosts), including at least
        one host number high enough to require a two-letter *host* code
        (e.g. host `num` ≥ 27, producing a host letter like `AA`) to
        exercise the bijective base-26 rollover.
- [ ] For every store shape in the matrix, for every camera in the store,
      the test: (a) generates the code via `code_for(host_num, cam_num,
      is_local)` the same way `_list_all_from_store`/`_list_single_daemon`
      now do; (b) parses it back via `resolve_code(code, store)`; (c)
      asserts the returned `(host_entry, camera_entry)` matches the
      originating host and camera exactly (by identity of stable
      identifying fields, e.g. `host_entry["host"]` and
      `camera_entry["num"]`/`["enum"]`).
- [ ] `grep -rn "resolve_host_letter_number\|TestResolveHostLetterNumber"
      src/ tests/` returns zero matches anywhere in the repository.
- [ ] Full test suite (`uv run pytest`) passes green.
- [ ] A brief manual/fixture-based check (not necessarily live hardware)
      confirms the end-to-end story: a code printed by
      `_list_all_from_store` for a synthetic multi-host store, when passed
      through `cli/view_cli.py`'s (now single-stage) resolution path against
      a fake `DaemonControl`, resolves to the correct host + camera enum —
      i.e. the producer (`cameras`) and consumer (`view`) genuinely agree
      end-to-end, not just at the `host_codes.py` unit level.

## Implementation Plan

**Approach**: Property-style/matrix unit tests at the `host_codes.py` layer
(fast, no daemon needed) plus one integration-shaped test that exercises
the full `cameras`-emits → `view`-parses path against fakes/fixtures, per
the sprint's Test Strategy ("the `host_codes`/`_daemon` layer tests are the
primary regression coverage since both commands delegate to that layer
after this change").

**Files to create/modify**:
- `tests/test_host_codes.py` — add a `TestRoundTrip` (or similarly named)
  class implementing the store-shape matrix described in Acceptance
  Criteria. Reuse existing store-fixture helpers in the file
  (`_make_store` or equivalent) where possible; extend them if they don't
  yet support multi-remote/high-host-number shapes.
- Confirm (do not re-delete) that `TestResolveHostLetterNumber` is absent
  per ticket 002's cleanup; if for any reason it is still present (ticket
  002 landed differently than planned), remove it here as a safety net.
- Optionally, a small end-to-end fixture test in `tests/test_view_cli.py`
  (new file, if one doesn't exist) or alongside existing CLI tests,
  exercising `view_cli.py`'s code-resolution path with a fake
  `DaemonControl`/`connect_from_args` against a synthetic store — confirming
  the code `cameras` would print for that store opens the right camera via
  `view`.

**Testing plan**:
- `uv run pytest tests/test_host_codes.py -v` — new matrix tests pass.
- `uv run pytest` (full suite) — green, no regressions from tickets
  001-003's changes.
- Manually sanity-check (during implementation, not required as an
  automated test) one multi-host scenario matches the sprint's original
  repro shape from the issue (host B with cameras including one at a
  higher enum) to confirm the fix addresses the literal reported case.

**Documentation updates**: None beyond what tickets 002/003 already cover.
This ticket is test-only.
