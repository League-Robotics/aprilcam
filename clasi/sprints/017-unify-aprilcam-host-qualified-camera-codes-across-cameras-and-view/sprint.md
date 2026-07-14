---
id: '017'
title: Unify aprilcam host-qualified camera codes across cameras and view
status: architecture-review
branch: sprint/017-unify-aprilcam-host-qualified-camera-codes-across-cameras-and-view
use-cases:
- SUC-001
- SUC-002
- SUC-003
issues:
- aprilcam-view-cannot-parse-host-qualified-camera-codes.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 017: Unify aprilcam host-qualified camera codes across cameras and view

## Goals

Make `aprilcam view <code>` accept exactly the camera codes that
`aprilcam cameras` prints, on both local-only and network (multi-host)
listings, by unifying two divergent host-qualified camera-code schemes into
one canonical, shared format+parse implementation.

## Problem

`aprilcam cameras` lists cameras across hosts using letter+digit codes
(e.g. `B6` = camera 6 on host B) and its own help text tells the user to run
`aprilcam view B6`. But `aprilcam view B6` fails with `Error: no camera
numbered 6 on the daemon` — the host-letter prefix is dropped, the bare
digit is queried against the *local* daemon instead of host B's, and no
validation ever confirms camera 6 exists on host B in the first place.

Investigation (issue + this sprint's own source-code verification) found
**two divergent, coexisting host-code schemes** that were never reconciled
by a shared helper:

- **letter+letter** (`A`, `FB`) — `client/host_codes.py`'s `code_for()` /
  `resolve_code()`. Validates the camera exists on the resolved host,
  correctly translates the store's stable `num` to the daemon's live
  `enum`, and is already the scheme documented in `docs/wiki/daemon.md` and
  already used correctly by both `cli/view_cli.py` and `cli/tags_cli.py`.
- **letter+digit** (`B6`) — `cli/cameras_cli.py`'s network-listing path and
  `cli/_daemon.py:resolve_host_letter_number`. Does not validate the camera
  against the host, does no store-num↔daemon-enum translation, and has a
  local-kind shortcut that skips reconnecting — so a genuine remote code
  can silently resolve against the *local* daemon. This is also reachable
  *before* the working letter+letter resolution in `view_cli.py`, shadowing
  it for any argument shaped like `[A-Za-z]\d+`.

`aprilcam cameras` is internally inconsistent too: its network-listing path
prints letter+digit while its single-daemon listing path prints
letter+letter via `code_for` — so the command doesn't even agree with
itself.

## Solution

Canonicalize on the **letter+letter** scheme (`code_for` / `resolve_code`)
— already the documented, tested, and correctly-implemented scheme in two
of three call sites — and retire the letter+digit scheme entirely rather
than patching it:

1. Delete `cli/_daemon.py:resolve_host_letter_number` and the letter+digit
   resolution block it drives in `cli/view_cli.py`. `resolve_camera_code`
   (already using `resolve_code`) becomes the sole camera-code resolver for
   both `view` and `tags`.
2. Add `client/host_codes.py:resolve_code_or_none`, a thin non-raising
   wrapper `resolve_camera_code` uses in place of its current broad
   `except (ValueError, Exception)`.
3. Fix `cli/cameras_cli.py:_list_all_from_store` to format codes via
   `code_for` (the same function `_list_single_daemon` already uses)
   instead of its ad hoc letter+digit f-string, and update the printed
   usage-hint example to a working letter+letter code.
4. Add a round-trip test matrix: for local-only, single-remote, and
   multi-remote store shapes, every code the listing logic would print via
   `code_for` must parse back through `resolve_code` to the exact
   `(host, camera)` pair it was generated from.

Full design detail, codebase-verification citations, and rationale for the
canonical-scheme choice are in `architecture-update.md`.

## Success Criteria

- SUC-001: every code `aprilcam cameras` prints (local-only or network
  mode) succeeds when passed to `aprilcam view <code>`, routes to the
  correct host's daemon, and opens the correct camera. A code addressing a
  remote camera always reconnects to that host's daemon — never silently
  falls back to the local daemon.
- SUC-002: `aprilcam cameras`' network-listing path and single-daemon path
  format camera codes identically (same function, same scheme); the
  printed usage hint is copy-pasteable and works.
- SUC-003: there is exactly one camera-code format+parse implementation in
  the codebase (no divergent letter+digit parser survives); a round-trip
  test suite proves every code the listing logic can emit parses back to
  the correct `(host, camera)` pair across a matrix of store shapes.

## Scope

### In Scope

- `client/host_codes.py`: add `resolve_code_or_none` (additive; no changes
  to existing `code_for`/`resolve_code`/`resolve_host_token`).
- `cli/_daemon.py`: delete `resolve_host_letter_number`; narrow
  `resolve_camera_code`'s exception handling.
- `cli/view_cli.py`: delete the letter+digit resolution block; single-stage
  code resolution via `resolve_camera_code`.
- `cli/cameras_cli.py`: `_list_all_from_store` formats codes via `code_for`;
  usage-hint text updated to a working example.
- Test suite: remove `TestResolveHostLetterNumber` (tests deleted code);
  add `resolve_code_or_none` tests; add the round-trip test matrix.

### Out of Scope

- Any daemon-side or wire-protocol (proto) change — this sprint is entirely
  within the CLI client layer (`cli/`, `client/`), reusing
  `client/discovery.py:resolve_daemon_target` and `connect_from_args`
  unchanged.
- Expanding the base-26 addressing limit (≤26 hosts, ≤26 cameras/host) —
  no evidence any deployment is near this ceiling; would be speculative
  generality.
- A "did you mean the new format?" hint for a `B6`-shaped argument that
  fails to resolve after this sprint — left as an open question /
  implementer's discretion during ticketing, not required for correctness.
- Any change to `cli/tags_cli.py` — already uses the canonical scheme
  correctly; included in verification only.

## Test Strategy

- Unit tests for `resolve_code_or_none` mirroring existing `resolve_code`
  coverage in `tests/test_host_codes.py`, plus the non-raising case.
- Round-trip test matrix: generate codes via `code_for` for local-only,
  one-remote, and multi-remote (including a host number requiring a
  two-letter host code) store shapes; assert each parses back through
  `resolve_code` to the originating `(host, camera)` pair.
- Remove `TestResolveHostLetterNumber` (tests the deleted parser, including
  the test — `test_local_code_no_reconnect` — that documents the bug being
  fixed).
- All existing tests remain green, including current `code_for`/
  `resolve_code`/`resolve_host_token` coverage (unchanged functions).
- No dedicated `view_cli.py`/`cameras_cli.py` unit tests exist today for
  code resolution; the `host_codes`/`_daemon` layer tests are the primary
  regression coverage since both commands delegate to that layer after
  this change.

## Architecture Notes

Canonical scheme: **letter+letter** (`code_for`/`resolve_code`), not
letter+digit. Chosen because it is already the scheme documented in
`docs/wiki/daemon.md`, already fully tested, already correctly implemented
(validates the camera against the host, translates store `num` to daemon
`enum`), and already the live implementation in `view_cli.py` and
`tags_cli.py`. The letter+digit scheme is deleted rather than repaired,
since patching it in place would leave two parsers for one concept — the
exact structural problem the issue identifies as root cause. See
`architecture-update.md` for the full "Canonical Scheme Decision" section,
component/dependency diagrams, and design rationale. No changes cross the
daemon boundary; the Sprint 014 discovery/connection model
(`resolve_daemon_target` precedence: explicit flag → env/config →
local Unix socket → mDNS) is reused unchanged.

## GitHub Issues

(none yet)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [x] Sprint planning documents are complete (sprint.md, use cases, architecture)
- [x] Architecture review passed (APPROVE)
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | Add resolve_code_or_none and narrow resolve_camera_code's exception handling | — |
| 002 | Delete resolve_host_letter_number and collapse view's code resolution to one stage | 001 |
| 003 | Fix cameras' network listing to format codes via code_for | 001 |
| 004 | Round-trip test matrix and removal of tests for the deleted parser | 002, 003 |

Tickets execute serially in the order listed. Ticket creation is pending
stakeholder approval of this sprint plan (see Definition of Ready above).
