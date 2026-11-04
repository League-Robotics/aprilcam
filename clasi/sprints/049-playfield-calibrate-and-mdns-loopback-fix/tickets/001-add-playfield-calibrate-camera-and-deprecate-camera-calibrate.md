---
id: '001'
title: Add playfield calibrate [CAMERA] and deprecate camera calibrate
status: open
use-cases: ["SUC-001", "SUC-002"]
depends-on: []
github-issue: ''
issue: move-calibrate-to-playfield-calibrate.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Add playfield calibrate [CAMERA] and deprecate camera calibrate

## Description

Add `aprilcam playfield calibrate [CAMERA]` and make `camera calibrate` a
deprecated alias. See sprint.md Architecture ("What Changed").

Working-tree note: master has UNCOMMITTED changes in
`src/aprilcam/cli/playfield.py` and `tests/aprilcam/cli/test_host_n_cli.py`
(a `_connect()` helper giving `playfield image`/`view` HOST-N support via
`connect_for_camera`; 45 tests pass). **Commit that existing change as part
of this ticket**, and make `playfield calibrate` reuse `_connect`. Do NOT
touch or commit `AGENTS.md`, `uv.lock`, or
`docs/knowledge/2026-11-02-daemon-cpu-cost-per-frame.md`.

Design (stakeholder-fixed): CAMERA is an optional positional (`nargs='?'`),
not `--camera`. It accepts slug, persistent number, or HOST-N (`angst-4`).
Omitted: `resolve_playfield_camera`. Keep the name `calibrate`; results stay
per camera; daemon RPC unchanged. `-v`/`--all` behaviour as today
(`--all` refused with the existing message, reworded for the new command
where it names the command).

Implementation approach:
- `cli/_common.py`: add keyword `hint: str = "--camera"` to
  `resolve_playfield_camera`, used in both error strings (`"... or pass
  {hint}"`, `"-- pass {hint} to disambiguate"`). Default keeps
  `playfield image`/`view` wording byte-identical.
- `cli/calibrate.py`: split `cmd_calibrate` so the connect-and-render body is
  shared; add `cmd_playfield_calibrate` (or equivalent) that, with CAMERA
  omitted, does `discovery_factory().connect(args.daemon)` ->
  `resolve_playfield_camera(daemon, hint="CAMERA")` -> error via
  `report_error`/stderr + `EXIT_ERROR`; with CAMERA given uses
  `playfield._connect`/`connect_for_camera`. Avoid import cycles
  (`playfield.py` importing `calibrate.py` is fine; reuse `_connect` by
  importing from `playfield` or moving it to `_common`).
- `cli/playfield.py`: register `calibrate` subparser with positional
  `camera` (`nargs='?'`), `--all` refused, `-v` via existing mechanism;
  help text describes pose against the playfield's surveyed tags.
- `cli/camera.py`/`cli/calibrate.py`: `camera calibrate` help starts with
  "DEPRECATED: use 'aprilcam playfield calibrate'"; handler prints a
  deprecation line to stderr, then runs the unchanged behaviour (its
  `camera` stays a required positional).
- Check the MCP `calibrate` tool description (`mcp/tools/configuration.py`)
  describes a playfield pose operation; adjust wording only if it does not.

## Acceptance Criteria

- [ ] `aprilcam playfield calibrate --help` shows positional `CAMERA`
      (optional) and no `--camera` option.
- [ ] `playfield calibrate angst-4` connects to angst's daemon and sends
      camera `4` (FakeDiscovery routing test).
- [ ] Slug and persistent number work; `-d` handled as for other commands.
- [ ] Omitted CAMERA: exactly one linked present camera is used; zero and
      several each exit 1 with a message that says "pass CAMERA" (and lists
      candidates for several).
- [ ] `playfield image`/`view` resolver error text still says `--camera`.
- [ ] `--all` is refused before connecting.
- [ ] `camera calibrate X` writes a deprecation line naming `aprilcam
      playfield calibrate` to stderr, then produces the same stdout/exit code
      as before; its `--help` says deprecated.
- [ ] The uncommitted `_connect` change and its tests are committed with
      this ticket; unrelated working-tree files are not.
- [ ] MCP `calibrate` description reads as a playfield pose operation.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/cli/test_host_n_cli.py tests/aprilcam/cli/test_calibrate_cli.py tests/aprilcam/cli/test_playfield_cli.py tests/aprilcam/mcp -q`
- **New tests to write**: in `tests/aprilcam/cli/test_calibrate_cli.py`
  and/or `test_host_n_cli.py` (use the `FakeDiscovery` pattern there):
  positional parse + help, HOST-N routing, omitted-camera one/zero/several,
  hint wording both ways, deprecated alias warning + unchanged behaviour,
  `--all` refusal.
- **Verification command**: the existing-tests command above (scoped; the
  full suite runs once in close_sprint).
