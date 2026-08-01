---
id: '002'
title: Camera identity and persistent registry
status: done
use-cases:
- UC-14
- UC-25
depends-on:
- '001'
github-issue: ''
issue: daemon-bring-up-with-basic-camera-cli.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Camera identity and persistent registry

## Description

Depends on ticket 001 (`config/directories.py`'s `state_dir` is where
`registry.json` lives). Implements `camera/identity.py`
(`CameraIdentity` Protocol) and `camera/registry.py`
(`CameraRegistry` Protocol) from `src/aprilcam/camera/interfaces.py`.

`CameraIdentity` derives a stable slug from a camera's fallback chain:
libcamera id → serial/AVFoundation unique id → `vid:pid@location` →
device-name-plus-resolution slug. `CameraRegistry` persists identity →
stable enumeration number + camera directory in `registry.json` under
`state_dir`, atomically (tmp + fsync + rename per
`docs/design/design.md` §6), with enumeration numbers assigned once
and never reshuffled.

**Reuse from archive**: convert `archive/src/aprilcam/camera/identity.py`
(407 lines — the fallback chain, macOS `system_profiler` parsing, cv2
enumeration) and `archive/src/aprilcam/camera/registry.py` (457 lines —
`CameraRecord`, `resolve_enum_to_index`, stable-enumeration logic,
JSON-backed persistence) into the new Protocol shapes. Both are
"directly reusable" per the archive survey — the core logic matches the
new interfaces closely; conversion is primarily renaming to match exact
Protocol method signatures (`resolve(identity) -> str`,
`forget(slug) -> None`) and wiring to `config/directories.py`'s
`state_dir` instead of v1's directory resolution.

This ticket does **not** implement `camera/discovery.py`'s active
`probe()` or `watch()` — that is ticket 003's `CameraDiscovery`, built
on top of `Camera`/backends which this ticket does not yet have. This
ticket's registry logic is tested against **recorded enumeration-table
fixtures**, not live hardware — no `@pytest.mark.camera` tests here.

## Acceptance Criteria

- [x] `CameraIdentity.slug` / `.device_name` produce the documented
      readable slugs (e.g. `arducam-ov9782-usb-camera`) from a fallback
      chain, tested against recorded enumeration snapshots (fixtures),
      no hardware.
- [x] `CameraRegistry.resolve(identity)` is idempotent: the same
      physical identity always resolves to the same slug and the same
      enumeration number across repeated calls and across a simulated
      registry reload (UC-01.S-1's restart-stability property, tested
      headless via save/reload of `registry.json`).
- [x] Enumeration numbers are monotonic and never reshuffle when a new
      camera is added to an existing registry (`CameraState.number`
      contract in `types.py`).
- [x] `CameraRegistry.forget(slug)` removes an entry outright, distinct
      from an absent-but-remembered camera (which keeps its entry per
      UC-25 — "a config outliving its hardware is normal").
- [x] Atomic-write crash safety: a simulated write-then-kill leaves
      `registry.json` in its prior valid state, never partially
      written (per `design.md` §6's atomic-write convention).
- [x] Two cameras with names that would collide at the last-resort
      fallback slug are disambiguated, not silently merged into one
      registry entry (UC-25's open-question caveat, resolved here as:
      append a disambiguating suffix rather than merge).

## Testing

- **Existing tests to run**: `tests/aprilcam/config/test_directories.py`
  (ticket 001) — registry depends on `state_dir` resolution.
- **New tests to write**: `tests/aprilcam/camera/test_identity.py`
  (fallback chain over recorded `system_profiler`/enumeration snapshot
  fixtures — no hardware); `tests/aprilcam/camera/test_registry.py`
  (idempotent resolve, stable numbering, atomic-write crash injection,
  forget vs absent-but-remembered, collision disambiguation) — all
  headless, using `tmp_path` for the registry file.
- **Verification command**: `uv run pytest tests/aprilcam/camera/ -k "identity or registry"`
