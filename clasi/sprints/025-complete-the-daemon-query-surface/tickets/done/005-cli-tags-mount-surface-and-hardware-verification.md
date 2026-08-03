---
id: '005'
title: CLI tags mount surface and hardware verification
status: done
use-cases:
- SUC-002
depends-on:
- '004'
github-issue: ''
issue: complete-the-daemon-query-surface.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# CLI tags mount surface and hardware verification

## Description

Add `aprilcam tags mount register|clear|list` (new `cli/tags.py` — the
issue's own suggested spelling, chosen over `camera mount` since the
capability is about a *tag's* mount, not a *camera*'s configuration).
`tags/` currently has zero implementation files (only
`tags/interfaces.py`'s `TagGenerator`); this module is new, and sprint
028's later `aprilcam tags generate` (UC-21) will extend this same
file rather than conflict with it.

- `aprilcam tags mount register <family> <number> [--x CM] [--y CM]
  [--z CM] [--yaw-deg DEG] [--size CM] [-d HOST]` — `family` is
  `apriltag`/`aruco` (matching `TagFamily`'s own enum string values
  exactly, no translation table needed), `number` an int. All offset
  flags optional, defaulting to `MountParameters`' own zero defaults;
  `--yaw-deg` converts degrees (CLI-friendly) to `mount_yaw_rad` (the
  wire/Python unit) at the CLI boundary only, mirroring how other CLI
  modules already do their own small unit conversions at the edge
  (`cli/playfield.py`'s module docstring: "each side owns its own
  small conversion").
- `aprilcam tags mount clear <family> <number> [-d HOST]` — calls
  `unregister_tag`; an absent registration is a no-op (prints
  confirmation either way, matching `cmd_annotations --clear`'s
  existing tone).
- `aprilcam tags mount list [-d HOST] [--all]` — calls
  `list_tag_parameters`; read-only, so it fans out like `playfield
  list`. An empty registry prints `(no registered tags)`, matching
  this codebase's established `(no X)` convention.

**Hardware verification** (the issue's own Verification bullet, made
concrete): with camera 3 (`arducam-ov9782-usb-camera`) calibrated per
the current field state (~0.18cm reprojection error, all 10 ArUco +
AprilTag 1 visible), physically place a tag at a known elevation on a
small stand, register a mount for it via the CLI, and confirm
`aprilcam camera tags` (or `get_tag`) reports a position measurably
different from — and closer to the tag's actual ground footprint
than — the unregistered raw position; unregister and confirm it
reverts. `@pytest.mark.camera`, skips cleanly without hardware.

If any test disturbs camera 3's settings, restore the baseline
(`auto-exposure-mode=1, exposure-time-abs=6, gain=2,
auto-white-balance-temp=false, white-balance-temp=4600`) before
finishing.

## Acceptance Criteria

- [x] `aprilcam tags mount register|clear|list` implemented in
      `cli/tags.py`, wired into the top-level parser.
- [x] `register` accepts `--x`/`--y`/`--z`/`--yaw-deg`/`--size`, all
      optional; `--yaw-deg` converts to radians before calling
      `register_tag`.
- [x] `clear` on an unregistered tag succeeds as a no-op (no error).
- [x] `list` prints `(no registered tags)` when the registry is empty,
      and one row per registration otherwise.
- [x] Hardware-verified (`@pytest.mark.camera`): registering a mount
      via the CLI changes the reported world position by
      approximately the registered offset (compared against the same
      tag's unregistered reading); `tags mount clear` reverts it. Test
      skips cleanly with no camera attached. **Scoped to
      `mount_x`/`mount_y`/`mount_yaw_rad` only** — see the "mount_z
      scope note" below; `mount_z`/parallax hardware verification is
      explicitly **blocked by ticket 004's stopgap**
      (`DaemonService._camera_geometry_for` returns height `0.0` for
      every camera, so parallax correction has no real effect on
      hardware yet), not silently skipped. Both the automated test
      (`tests/aprilcam/daemon/test_mobile_tags_hardware.py`) and a
      live manual CLI pass against the real system daemon confirmed
      x/y/yaw offsets move the reported position/heading and revert
      cleanly on `clear` — real numbers in this ticket's completion
      notes below.
- [x] Camera 3's baseline settings (`auto-exposure-mode=1,
      exposure-time-abs=6, gain=2, auto-white-balance-temp=false,
      white-balance-temp=4600`) are unchanged after the hardware test
      runs (asserted or manually restored). Verified via `uvc-util`
      readback after the full hardware suite ran: unchanged.
- [x] `uv run mypy` clean.
- [x] Full suite green, both headless (`-m "not camera"`) and with
      hardware attached (baseline 832 tests: 795 headless + 37
      hardware, plus everything this sprint adds). Measured: 942
      passed headless (`-m "not camera"`), 37 passed + 2 skipped with
      hardware attached (`-m camera`), 0 failed.

### mount_z scope note (real limitation, not silently dropped)

Ticket 004 found that `Calibration` has no camera-geometry field
anywhere in the codebase (no camera ground position or height above
the field is modeled) and shipped a stopgap:
`DaemonService._camera_geometry_for` returns `(WorldPoint(0, 0), 0.0)`
for every camera, where height `0.0` is
`project_tag_to_field_plane`'s own documented "nothing to correct"
sentinel. **Consequence: `mount_z` (parallax correction) has no real,
observable effect on hardware today.** This ticket's hardware
verification is therefore scoped to `mount_x`/`mount_y`/
`mount_yaw_rad` only, which do have real, observable effect and were
verified live (see completion notes). `mount_z`/parallax hardware
verification is **out of scope here, blocked by the ticket 004
stopgap** — a real follow-up (adding camera position/height to
`CameraConfig` + proto/loader plumbing) is needed before it can be
verified on hardware, and is recorded as a follow-up for a future
ticket, not attempted or faked here.

## Implementation Plan

**Approach**: CLI argv-wiring tests first (mocked `Discovery`/
`Daemon`, no hardware, mirroring
`tests/aprilcam/cli/test_playfield_cli.py`'s existing pattern), then
the hardware verification test last, since it depends on ticket 004's
daemon-side registry actually being live.

**Files**: `src/aprilcam/cli/tags.py` (new),
`src/aprilcam/cli/__init__.py` (wire the new subparser, mirroring how
`camera`/`daemon`/`playfield`/`calibrate` are already wired).

**Testing plan**:
- **Existing tests to run**: `uv run pytest tests/aprilcam/cli/ -v`,
  plus the full headless suite.
- **New tests to write**: `tests/aprilcam/cli/test_tags_cli.py` (argv
  wiring for register/clear/list, mocked daemon); a new hardware test
  (e.g. `tests/aprilcam/daemon/test_mobile_tags_hardware.py`, mirroring
  `test_calibration_hardware.py`'s existing pattern) for the
  registration-changes-position claim.
- **Verification command**: `uv run pytest -m "not camera"` for
  headless CI; full `uv run pytest` (both marks) on this development
  machine to exercise the hardware path; `uv run mypy`.

**Documentation updates**: none beyond CLI help strings
(self-documenting, matching every other CLI module's convention in
this codebase).

## Completion Notes

**CLI spelling**: `aprilcam tags mount register|clear|list` in new
`src/aprilcam/cli/tags.py`, wired into `src/aprilcam/cli/__init__.py`.
Chosen over `camera mount` because the capability describes a *tag's*
mount (size, offset, yaw on its robot), not a *camera's*
configuration — `register_tag`/`unregister_tag`/`list_tag_parameters`
never take a camera argument at all.

**Real hardware verification** (camera 3, `arducam-ov9782-usb-camera`,
calibrated against real `main-playfield`), scoped to
`mount_x`/`mount_y`/`mount_yaw_rad` only per the mount_z scope note
above:

- Automated (`tests/aprilcam/daemon/test_mobile_tags_hardware.py`,
  `@pytest.mark.camera`, real subprocess CLI against a private real
  daemon): registered `--x 8 --y 3` on whichever tag the camera
  currently saw (adaptive by design — the field's mobile AprilTag prop
  is not always in frame, and the correction math is identical for any
  `TagId`). One real captured run: target `aruco 7`, before
  `(-66.9, -44.7, yaw=0.013)`, after registering `--x 8 --y 3`
  `(-74.9, -47.7, yaw=0.009)`, reverted after `tags mount clear`
  `(-66.9, -44.7)`.
- Manual, live, against the real running system daemon (separate pass,
  `aruco 4` and AprilTag 1 both used at different points):
  - Position offset (`--x 15 --y 5` on `aruco 4`): before
    `(67.1, 0.1)`, after `(52.3, -5.1)`, reverted `(67.2, 0.1)`.
  - Yaw offset (`--yaw-deg 45` on `aruco 4`): before `yaw=0.012`, after
    `yaw=-0.769` (expected ≈ `0.012 - radians(45) = -0.773`), reverted
    `yaw≈0.003-0.017`.
  - Camera 3's UVC settings read back via `uvc-util` before and after:
    unchanged (`auto-exposure-mode=1, exposure-time-abs=6, gain=2,
    auto-white-balance-temp=false, white-balance-temp=4600`).

**Test results**: `uv run pytest -m "not camera"` → 942 passed, 39
deselected. `uv run mypy` → clean (57 source files). `uv run pytest -m
camera` → 37 passed, 2 skipped, 0 failed (one clean full run; two
earlier full runs each hit a single, different, non-reproducible
hardware flake — a calibration subprocess timeout once, the mobile
AprilTag-1 prop not being in frame once, a UVC exposure write
rejection on an unrelated camera once — each confirmed to pass cleanly
in isolated retry and each in a module this ticket does not own
(`test_detection_hardware.py`, `test_service.py`, both pre-existing
from earlier sprints), consistent with real-camera/lighting flakiness
rather than a regression from this ticket's changes).

**Chain status**: `find_feature` (001→002) and the mobile-tag mount
chain (003→004→005) are both now fully done. Only the guide chain
(006→007) remains open in sprint 025.
