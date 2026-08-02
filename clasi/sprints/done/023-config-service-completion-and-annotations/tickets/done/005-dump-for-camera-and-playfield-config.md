---
id: '005'
title: Dump for camera and playfield config
status: done
use-cases:
- SUC-004
depends-on:
- '002'
github-issue: ''
issue: config-service-completion-and-annotations.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Dump for camera and playfield config

## Description

`GetCameraConfig`/`GetPlayfield` RPCs already exist and already return
documents that round-trip via `to_dict()`
(`config.interfaces.ConfigDocument.to_dict`,
`config.loader.PlayfieldDefinition.to_dict`). Nothing in the CLI reads
them back — `cli/camera.py`'s `config` command is push-only;
`cli/playfield.py`'s `config` command is push-only too, and `playfield`
has **no read path at all** yet (no `get`/`list`/`--dump`). This ticket
adds `--dump` to both, plus the underlying `playfield` read wiring.

Depends on ticket 002 because an unconfigured camera's dump must return
the accurate self-describing template (UC-24.S-3), not the placeholder
two-key document.

## Acceptance Criteria

- [x] `aprilcam camera config <camera> --dump` prints the live document
      to stdout in exact on-disk JSON (`to_dict()` output, including
      `name`).
- [x] `aprilcam playfield config <name> --dump` does the same for a
      playfield (first CLI read path for `playfield config`).
- [x] UC-24.S-1 verified live: dump camera 3's live config, install it
      in `config_dir`, restart the daemon, dump again — byte-for-byte
      identical.
- [x] UC-24.S-3 verified: dumping an unconfigured camera returns the
      blank template (ticket 002), not an error.
- [x] UC-24.S-4 verified: dumping an unknown camera/playfield names
      what the daemon actually has (reuses the existing
      `UnknownCamera`/`UnknownPlayfield` error shape).
- [x] `uv run mypy` clean; baseline 629 passed / 1 skipped stays green;
      zero `typing.Protocol`.

## Implementation Plan

**Files to modify:**
- `src/aprilcam/cli/camera.py` — `cmd_config`: `--dump` branch
  (mutually exclusive with the positional `file`) calling
  `discovery.connect(...).get_camera_config(camera)` and printing
  `json.dumps(config.to_dict(), indent=2, sort_keys=True)`.
- `src/aprilcam/cli/playfield.py` — same shape for `playfield config
  --dump`, plus wiring `discovery.connect(...).get_playfield(name)`.

**Archive-reuse pointers:** none — `to_dict()`/`ConfigLoader` round trip
already exists and is already tested
(`tests/aprilcam/config/test_loader.py`); this ticket is CLI plumbing
only.

## Testing

- **Existing tests to run**: `tests/aprilcam/cli/test_camera_cli.py`,
  `tests/aprilcam/cli/test_playfield_cli.py`, full `uv run pytest` for
  the 629/1 baseline.
- **New tests to write**: headless — CLI test asserting `--dump` output
  round-trips through `ConfigLoader.load_camera_config`/
  `load_playfield` back to an identical document (against a fake
  `Daemon`). `@pytest.mark.camera` — UC-24.S-1's full dump → install →
  restart → dump cycle on camera 3.
- **Verification command**: `uv run pytest`; `uv run pytest -m camera`;
  `uv run mypy`.
