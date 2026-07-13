---
id: '003'
title: 'Data migration: playfield.json to playfields/main-playfield.json + 3 config.json
  files'
status: done
use-cases:
- SUC-005
depends-on:
- '001'
- '002'
github-issue: ''
issue: ''
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Data migration: playfield.json to playfields/main-playfield.json + 3 config.json files

## Description

One-time data migration. Moves the existing `data/aprilcam/playfield.json` into
the new `data/aprilcam/playfields/` directory as `main-playfield.json`, adds the
required identity fields, writes `config.json` into the three already-calibrated
camera directories, and clears stale `paths.json` files.

This migration must run AFTER tickets 001 and 002 because: (a) the
`PlayfieldDefinitionRegistry` must be able to load the result (ticket 002), and
(b) `save_camera_config` is used to write the `config.json` files (ticket 001).

### Migration steps (implemented as code, not ad-hoc shell commands)

1. Create `data/aprilcam/playfields/` directory.
2. Read `data/aprilcam/playfield.json`.
3. Add `"name": "main-playfield"` and `"display_name": "Main Playfield"` to the
   top-level object (before all other keys, for readability).
4. Write the result as `data/aprilcam/playfields/main-playfield.json` atomically.
5. Track `data/aprilcam/playfield.json` removal in git:
   `git mv data/aprilcam/playfield.json data/aprilcam/playfields/main-playfield.json`
   (use git mv so history is preserved).
6. For each of the three camera directories:
   - `data/aprilcam/cameras/arducam-ov9782-usb-camera/`
   - `data/aprilcam/cameras/hd-usb-camera/`
   - `data/aprilcam/cameras/global-shutter-camera/`
   Write `config.json` = `{"playfield": "main-playfield"}` using
   `save_camera_config`.
7. For each of the three directories, clear `paths.json` by writing `[]`
   atomically (same pattern as `_handle_open_camera`).

Do NOT rewrite or delete the existing `calibration.json` files in these
directories. They will be flagged as stale by mismatch detection (ticket 004)
because they predate provenance fields.

### Important: git mv, not copy

Use `git mv data/aprilcam/playfield.json data/aprilcam/playfields/main-playfield.json`
so git tracks the rename. Then write the modified content to the new location.
Do not `git add` the old path separately; `git mv` handles removal.

## Acceptance Criteria

- [x] `data/aprilcam/playfields/main-playfield.json` exists with `name:
      "main-playfield"` and `display_name: "Main Playfield"` at the top level,
      and all existing content from `playfield.json` intact.
- [x] `data/aprilcam/playfield.json` is absent from the repository (confirmed
      by `git status` showing the rename).
- [x] `data/aprilcam/cameras/arducam-ov9782-usb-camera/config.json` contains
      `{"playfield": "main-playfield"}`.
- [x] Same for `hd-usb-camera/config.json` and `global-shutter-camera/config.json`.
- [x] All three camera `paths.json` files contain `[]`.
- [x] `PlayfieldDefinitionRegistry.load_all(playfields_dir)` returns exactly one
      entry named `main-playfield` (run in a quick smoke test or via pytest).
- [x] Existing `calibration.json` files in the three directories are unchanged.
- [x] `uv run pytest` passes with the migration in place.

## Implementation Plan

### Approach

Write a small migration Python script or implement directly as ordered code
in the ticket. Use git mv for the rename. Use existing helpers from tickets
001 and 002.

### Files to create

- `data/aprilcam/playfields/main-playfield.json` (created by migration)
- `data/aprilcam/cameras/arducam-ov9782-usb-camera/config.json`
- `data/aprilcam/cameras/hd-usb-camera/config.json`
- `data/aprilcam/cameras/global-shutter-camera/config.json`

### Files to modify / remove

- `data/aprilcam/playfield.json` — moved via `git mv`; do not edit in place.
- `data/aprilcam/cameras/arducam-ov9782-usb-camera/paths.json` — write `[]`
- `data/aprilcam/cameras/hd-usb-camera/paths.json` — write `[]` (if exists)
- `data/aprilcam/cameras/global-shutter-camera/paths.json` — write `[]` (if exists)

### Testing plan

- After migration, run `uv run pytest` to confirm no regressions.
- Manual smoke test: `python -c "from aprilcam.core.playfield_def import
  PlayfieldDefinitionRegistry; r = PlayfieldDefinitionRegistry();
  r.load_all(Path('data/aprilcam/playfields')); print(r.list())"` — should
  print `['main-playfield']`.
- Manual check: `python -c "from aprilcam.camera.camera_config import
  load_camera_config; from pathlib import Path;
  print(load_camera_config(Path('data/aprilcam/cameras/arducam-ov9782-usb-camera')))"`.

### Documentation updates

None for this ticket.
