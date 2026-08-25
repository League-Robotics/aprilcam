---
id: '007'
title: Persist mobile-tag mount registrations across daemon restarts
status: in-progress
use-cases:
- SUC-001
depends-on: []
github-issue: ''
issue: mount-registrations-should-persist-across-daemon-restarts.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Persist mobile-tag mount registrations across daemon restarts

## Description

`register_tag`/`unregister_tag`/`list_tag_parameters` (ticket 025-004,
UC-12) store mount registrations (`TagId -> MountParameters`) only in
`DaemonService._mount_params`, a plain in-memory dict — every daemon
restart silently discards them, and a query afterward returns the raw
tag's pose instead of the robot's, with no error or warning.

Persist mount registrations following the **exact existing pattern**
`daemon/service.py` already uses for `calibration.json`
(`_persist_calibration`/`_load_calibrations`, `_calibration_path`) —
same atomic tmp+fsync+`os.replace` write, same tolerant-of-corruption
load stance (never raises, logs a warning, daemon construction always
succeeds). This sprint's `design/` overlay
(`clasi/sprints/036-.../design/daemon-DESIGN.diff.md`,
`config-DESIGN.diff.md`, and `design.diff.md`) records the storage
location/format decision — read it before implementing:

- **Location**: `state_dir` (daemon-owned, regenerable), not
  `config_dir` (human-owned) — mounts are written by the daemon itself
  in response to an RPC, exactly like calibration, never hand-edited.
- **Path**: `state_dir/mounts/registry.json` — one file, one directory,
  mirroring `camera/registry.py`'s `<state_dir>/cameras/registry.json`
  sibling layout (not one file per tag — the whole registry is small
  and always read/written as a unit, same shape `register_tag`'s
  upsert-by-tag semantics already assume).
- **Format**: a flat JSON object keyed by a string encoding of `TagId`
  (e.g. `"apriltag:7"`, `f"{tag.family.value}:{tag.number}"`) mapping
  to `MountParameters`' fields (`mount_x`, `mount_y`, `mount_z`,
  `mount_yaw_rad`, `size_cm`) — no existing `to_dict`/`from_dict` on
  `TagId`/`MountParameters` to reuse; write the (de)serialization
  inline in `service.py`, mirroring `Calibration.to_dict`/`from_dict`'s
  shape rather than inventing a different convention.
- **No `--session-only` option** — decided during sprint planning:
  `unregister_tag` is sufficient for the "temporary experiment" case
  the issue's open question raised; adding an ephemeral-registration
  flag would be speculative generality for a need nobody has actually
  hit yet. Do not add one.
- **Load-at-startup precondition behavior — the trickiest part.** A
  reload must **bypass** `register_tag`'s normal validation
  (`NotCalibrated` raised for nonzero `mount_z` with zero calibrations
  present) exactly the way `_load_calibrations` bypasses
  `Calibrator.calibrate`'s validation on load — populate
  `self._mount_params` directly, never by calling `register_tag()`
  internally. A persisted nonzero-`mount_z` registration reloaded into
  a daemon with no calibration at all must not crash startup and must
  not silently vanish; it should behave exactly like a fresh
  registration made into that same daemon state today (registration
  stands, height correction inert until a camera is calibrated **and**
  located, `mount_z_applied` reports the truth dynamically at query
  time via the existing `_mount_z_applied` computation — nothing about
  that computation changes, since it already reads current calibration
  state, not anything stored on the registration itself).

## Acceptance Criteria

- [ ] New `_mounts_path()` returns `state_dir / "mounts" /
      "registry.json"`, mirroring `_calibration_path`'s shape.
- [ ] New `_persist_mounts()` atomically writes the **entire** current
      `self._mount_params` dict (tmp+fsync+`os.replace`, creating
      `state_dir/mounts/` on demand) — called from `register_tag`'s
      success path (after the existing precondition checks, alongside
      the existing `self._mount_params[tag] = params` line) and from
      `unregister_tag` (after `self._mount_params.pop(tag, None)`).
- [ ] New `_load_mounts()` reads `state_dir/mounts/registry.json` if
      present, tolerantly (missing file, corrupt JSON, or a
      malformed entry are each logged as a warning and skipped/ignored
      — never raised, never crashes daemon construction), and
      populates `self._mount_params` directly. Called from
      `DaemonService.__init__` (alongside the existing
      `self._load_calibrations()` call) — before any RPC can be
      served.
- [ ] `unregister_tag` on a tag with no persisted entry remains a
      no-op (existing behavior preserved) — `_persist_mounts()` after
      a no-op pop still succeeds (harmless rewrite of an unchanged
      file, or simply skip the write if nothing changed — implementer's
      choice, either is correct).
- [ ] `list_tag_parameters`'s `mount_z_applied` flag is unaffected by
      this change (it is already computed fresh from current
      calibration state at query time, not from anything this ticket
      stores) — confirm with a test rather than assuming.
- [ ] New tests (new file `tests/aprilcam/daemon/test_mount_persistence.py`
      or an addition to `tests/aprilcam/daemon/test_service.py`,
      implementer's choice) cover:
  - [ ] Register a mount, construct a **second** `DaemonService`
        against the same `state_dir` (simulating a restart), query —
        reports the corrected pose, not the raw tag pose.
  - [ ] Unregister, "restart," query — reports the raw tag pose (entry
        was actually removed from disk, not just from memory).
  - [ ] A missing `mounts/registry.json` (first run) loads cleanly
        with an empty `_mount_params`, no warning-as-error.
  - [ ] A corrupt `mounts/registry.json` loads cleanly (warning
        logged, entry skipped, daemon construction succeeds).
  - [ ] A persisted nonzero-`mount_z` registration reloaded into a
        `DaemonService` with **no** calibrations loaded at all does
        not raise `NotCalibrated` and is not dropped — it is present
        in `list_tag_parameters()`'s output with `mount_z_applied`
        reporting the correct (inert) state.

## Testing

- **Existing tests to run**:
  `uv run pytest tests/aprilcam/daemon/test_service.py -q`
- **New tests to write**: `tests/aprilcam/daemon/test_mount_persistence.py`
  (or additions to `test_service.py`) per the acceptance criteria
  above.
- **Verification command**:
  `uv run pytest tests/aprilcam/daemon/test_service.py tests/aprilcam/daemon/test_mount_persistence.py -q`
