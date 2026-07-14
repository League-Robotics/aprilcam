---
id: '003'
title: Fix cameras' network listing to format codes via code_for
status: in-progress
use-cases:
- SUC-002
depends-on:
- '001'
github-issue: ''
issue: aprilcam-view-cannot-parse-host-qualified-camera-codes.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Fix cameras' network listing to format codes via code_for

## Description

`aprilcam cameras` is internally inconsistent: its network-listing path
(`_list_all_from_store`, no `--host`) prints letter+digit codes via an ad
hoc f-string, while its single-daemon listing path (`_list_single_daemon`,
with `--host`) already prints letter+letter codes via `code_for`. This
ticket makes `_list_all_from_store` use `code_for` too, so both paths
produce the same code for the same store entry, and updates the printed
usage hint to a working letter+letter example.

This ticket is independent of ticket 002 (both depend only on ticket 001)
and can be done in either order relative to it, but both must land before
ticket 004's round-trip tests, which assert every code the listing logic
emits parses back correctly.

See `architecture-update.md` §4 ("`cli/cameras_cli.py` — network listing
switches to `code_for`") for full design detail.

## Acceptance Criteria

- [ ] `cli/cameras_cli.py:_list_all_from_store` formats each camera's code
      via `code_for(h_num, c["num"], is_local)` — the camera's *store* `num`
      (stable, letter-mapped), not the daemon's live `enum` — replacing the
      current `code = f"{num}" if is_local else f"{h_letter}{num}"` (which
      used the daemon `enum` directly, letter+digit style).
  - [ ] The daemon `enum` may still be displayed alongside the code for
        informational purposes (e.g. under `--details`), but the *code*
        itself is always `code_for`'s output.
- [ ] `_list_all_from_store` and `_list_single_daemon` produce byte-identical
      codes for the same host+camera store entry (verified by a test that
      constructs a matching store and compares both functions' output for
      several entries).
- [ ] The printed usage hint (currently `'aprilcam view B6'  (camera 6 on
      host B)'`) is replaced with a working letter+letter example consistent
      with `docs/wiki/daemon.md` (e.g. `'aprilcam view FB'  (camera 2 on
      host F)`), and this example, if run against the same fixture store
      used in tests, actually resolves via `resolve_code`.
- [ ] The local-camera example in the usage hint (`'aprilcam view 3' (local
      camera 3)`) is preserved or updated consistently — local cameras may
      be addressed by bare persistent enum (existing fallback) or by their
      single-letter code; verify which the codebase intends as primary and
      keep the hint accurate (cross-check against `_list_single_daemon`'s
      existing local example, if any, for consistency).
- [ ] No change to `_list_single_daemon` — it already uses `code_for`
      correctly.

## Implementation Plan

**Approach**: Replace the ad hoc code-formatting expression in
`_list_all_from_store` with a `code_for` call, threading the camera's store
`num` (already present on each camera dict per `host_codes.py`'s
`_merge_cameras` schema) through instead of the daemon `enum`. Update the
static usage-hint strings.

**Files to create/modify**:
- `src/aprilcam/cli/cameras_cli.py` — in `_list_all_from_store` (currently
  lines ~52-94): change the per-camera loop to call
  `code_for(h_num, c.get("num"), is_local)` for the printed code (import
  `code_for` — already imported at the top of the file per current source);
  keep displaying `c.get("enum")` separately if informational display is
  desired (see Open Question in architecture-update.md — implementer's
  discretion, keep it simple, no new flags). Update the two-line usage hint
  at the end of the function to a letter+letter worked example.
- Verify `code_for`/`num_to_alpha` imports already present in
  `cameras_cli.py` (confirmed present: `from ..client.host_codes import
  code_for, find_host, load_store, num_to_alpha`) — no new imports needed.

**Testing plan**:
- New test: construct a store fixture with a local host and 1-2 remote
  hosts, each with 2+ cameras with distinct `num`/`enum` values (to make
  sure the test would catch a `num`/`enum` mixup); call
  `_list_all_from_store` (capture stdout) and `_list_single_daemon`-equivalent
  code-generation for the same entries; assert the codes match.
- Test that the printed usage-hint text, when the example code is looked up
  in the same fixture store via `resolve_code`, resolves successfully.
- Run `uv run pytest tests/ -k cameras` and the full suite
  (`uv run pytest`) to confirm no regressions.

**Documentation updates**: None — `docs/wiki/daemon.md`'s example output
(`aprilcam cameras --host vidar.local` showing `[6] AA imx296-88000`) is
already letter+letter and already consistent with the fix; re-verify during
implementation that no other doc references the old `B6`-style output.
