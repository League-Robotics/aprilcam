---
id: '010'
title: Generalize hardware_camera.py rig enumeration across cameras 2, 3, 5
status: in-progress
use-cases:
- UC-22
depends-on: []
github-issue: ''
issue: aprilcam-v2-layered-test-system.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Generalize hardware_camera.py rig enumeration across cameras 2, 3, 5

## Description

`tests/aprilcam/hardware_camera.py` today resolves *the one* best
qualifying camera against a playfield (`resolve_hardware_camera_identity`/
`resolve_hardware_camera_slug`), used by eight existing hardware test
modules that each only needed one camera. This sprint's L1 hardware
flagship test (ticket 011) and the parameterized full-calibrate flow
need to enumerate and exercise **all** qualifying cameras (2, 3, 5), each
skipping individually with its own remediation message if unavailable —
not just the single best one.

This is additive: existing callers using
`resolve_hardware_camera_slug()`/`resolve_hardware_camera_identity()`
for "the one camera" keep their exact current contract and behavior.

## Acceptance Criteria

- [ ] A new function (e.g. `resolve_all_hardware_camera_identities(playfield=...)`)
      returns every currently-attached, configured-and-calibrated camera
      against `playfield` — not just the first/best one — using the same
      "genuinely qualifies" check (`config.json`'s `playfield` field +
      `calibration.json` presence) as the existing single-camera
      resolver.
- [ ] Existing single-camera callers (`resolve_hardware_camera_identity`,
      `resolve_hardware_camera_slug`) are unchanged in behavior — full
      existing hardware-test suite passes unmodified.
- [ ] A camera that qualifies but fails the availability probe is still
      included (candidates are not silently dropped for being busy — the
      per-camera skip decision belongs to the caller, per this module's
      existing "never silently substitute" philosophy), with the same
      informative print-diagnostics precedent this module already uses.
- [ ] If zero cameras qualify, the new function raises the same
      clean-skip contract (`pytest.skip` with remediation text) as the
      existing single-camera function — never a hard failure.
- [ ] `tests/aprilcam/calibration/` (or wherever the full-calibrate flow
      lives) gains a parameterized test iterating cameras 2, 3, 5 via the
      new enumeration, each camera skipping individually rather than one
      unavailable camera skipping the whole parameterized set.
- [ ] `--with-intrinsics` on the flat field reports
      `"coplanar-constrained"` for at least one of the three cameras
      (an existing behavior, re-asserted here across the newly
      parameterized set as a regression check).

## Implementation Plan

**Approach**: Add the multi-camera enumeration as a new function
alongside the existing single-camera ones, sharing their internal
qualification-check helpers (`_configured_playfield`, `_probe_available`)
rather than duplicating that logic.

**Files to modify**:
- `tests/aprilcam/hardware_camera.py` — new
  `resolve_all_hardware_camera_identities` function, `__all__` update.
- The full-calibrate hardware test module — parameterize over the new
  enumeration.

**Testing plan**:
- Run against whichever of cameras 2, 3, 5 are physically attached at
  test time; each unavailable camera's parameterized case skips
  individually.
- **Verification command**: `uv run pytest tests/aprilcam/hardware_camera.py tests/aprilcam/calibration/ -m camera -v`

**Documentation updates**:
- `tests/aprilcam/hardware_camera.py`'s own module docstring — one
  paragraph on the new multi-camera function, following its existing
  "why this exists" documentation style.
