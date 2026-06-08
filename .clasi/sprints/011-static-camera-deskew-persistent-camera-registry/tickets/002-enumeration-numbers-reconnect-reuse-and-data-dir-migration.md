---
id: '002'
title: Enumeration numbers, reconnect reuse, and data-dir migration
status: open
use-cases: [SUC-006]
depends-on: ['001']
github-issue: ''
issue: persistent-camera-registry-with-stable-identity-and-enumeration.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Enumeration numbers, reconnect reuse, and data-dir migration

## Description

Build the enumeration and reconnect logic on top of the registry schema from
ticket 001, and migrate existing per-camera data directories into the registry
without renaming them.

This ticket adds to `camera/registry.py`:

1. **Monotonic enumeration.** Assign each newly-seen camera the next
   enumeration number on first sight; persist `next_enum` in `registry.json`.
2. **Reconnect reuse.** A `resolve(device_or_index) -> record` API that looks
   the camera up by `unique_id` (from ticket 001's resolver) and reuses its
   existing enumeration number **and** its per-camera dir. A genuinely new
   `unique_id` gets a fresh number and record.
3. **Data-dir adoption/migration.** On first registry load, scan existing
   `data/aprilcam/cameras/<slug>/` directories and adopt them into records,
   matched by slug, with identity backfilled when the device is currently
   connected. Per the architecture decision, dirs are **not renamed** — the
   record's `dir` key keeps the existing slug dir so `calibration.json`,
   `paths.json`, and `info.json` paths stay valid. When two records would
   collide on the same slug dir (the identical-model case), disambiguate the
   newcomer's dir with an enum suffix (`<slug>-<enum>`) while leaving the
   already-populated dir untouched.

No daemon or CLI wiring here (ticket 003); this ticket is pure registry logic
plus migration, fully unit-testable against a temp data dir.

## Acceptance Criteria

- [ ] First sight of a camera assigns the next monotonic enumeration number;
  `next_enum` persists across reloads.
- [ ] `resolve` looks up by `unique_id` and returns the existing record (same
  enum number, same dir) for a known camera; a new `unique_id` yields a new
  record with the next number.
- [ ] Unplug + replug (simulated by resolving the same `unique_id` after a
  reload) reuses the enumeration number and dir — no new record, no renumber.
- [ ] Two identical-model cameras (same slug, distinct `unique_id`) get two
  records with distinct numbers; the second's dir is disambiguated and the
  first dir's existing data is untouched.
- [ ] Existing `data/aprilcam/cameras/<slug>/` dirs are adopted into records,
  not renamed or orphaned; their `calibration.json`/`paths.json`/`info.json`
  remain at their original paths.
- [ ] `uv run pytest` passes.

## Implementation Plan

- **Approach**: Extend `CameraRegistry` with `resolve`, enumeration counter,
  and a one-time `adopt_existing_dirs` migration run at load. Keep all logic
  filesystem-driven and dependency-free of the daemon.
- **Files to modify**: `src/aprilcam/camera/registry.py`.
- **Files to create**: none beyond test fixtures.
- **Testing plan**: see Testing; drive everything through a `tmp_path` data dir.
- **Docs**: document the no-rename adoption policy and the slug-collision
  disambiguation in the registry module docstring.

## Testing

- **Existing tests to run**: `uv run pytest tests -k registry`, then full
  `uv run pytest`.
- **New tests to write**:
  - enumeration: first sight assigns N, next camera N+1, `next_enum` survives
    reload.
  - reconnect reuse: resolve same `unique_id` twice (with a reload between)
    returns the same number/dir.
  - identical-model: two distinct `unique_id`s with the same slug get distinct
    numbers and disambiguated dirs; existing data in the first dir is preserved.
  - migration: seed a `tmp_path` cameras dir with a legacy `<slug>/` containing
    `calibration.json`; assert it is adopted (record created, file path
    unchanged, dir not renamed).
- **Verification command**: `uv run pytest`
