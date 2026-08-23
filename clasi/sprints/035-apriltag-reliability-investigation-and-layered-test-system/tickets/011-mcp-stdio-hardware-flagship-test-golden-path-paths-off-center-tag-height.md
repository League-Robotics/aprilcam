---
id: '011'
title: 'MCP stdio hardware flagship test: golden path, paths, off-center tag height'
status: open
use-cases: [UC-22, UC-12]
depends-on: ['009', '010']
github-issue: ''
issue: aprilcam-v2-layered-test-system.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# MCP stdio hardware flagship test: golden path, paths, off-center tag height

## Description

The flagship L1 hardware test: isolated `APRILCAM_CONFIG_DIR`/
`APRILCAM_STATE_DIR`/`XDG_RUNTIME_DIR` roots plus a private daemon plus a
real camera (via ticket 010's enumeration), driving the entire session
over a real MCP stdio connection (ticket 009's client). Never touches the
real `camera_position` — pushes a playfield + camera config with no
`camera_position`, then calibrates into the isolated state dir.

Three flows, one uninterrupted stdio session:
1. **Golden path** — `get_guide` → `list_cameras` → `get_tags` → `where`.
2. **Paths** — `draw_path` → record round-trip + revision check →
   `get_frame` deskewed → rendered-pixel assertion at the polyline
   midpoint → `clear_layer` → confirm gone.
3. **Off-center tag height / parallax** — `register_tag` with
   `mount_z=20` on a tag ~70 cm off-nadir → `mount_z_applied == False`
   (unconfigured camera, unmoved) → `set_camera_config` with the known
   real geometry for this isolated session → `mount_z_applied == True`,
   reported center moves per the parallax formula
   (`corrected = raw + (h/H)·(nadir − raw)`), hypot displacement > 2 cm,
   per-axis approx abs=2.0 → `unregister_tag` → position restored.

Displacement constants are imported from the same helper
`test_camera_geometry_hardware.py` uses — never redeclared — so the two
tests cannot drift apart.

## Acceptance Criteria

- [ ] The full session runs over one real `aprilcam mcp` stdio
      connection, against one real camera resolved via ticket 010's
      enumeration (parameterized across cameras 2, 3, 5, each skipping
      individually if unavailable).
- [ ] Every write in this flow (config push, calibration, mount
      registration) lands under the isolated
      `APRILCAM_CONFIG_DIR`/`APRILCAM_STATE_DIR` roots — spot-checked
      that the real `~/.config/aprilcam`/`~/.local/share/aprilcam` are
      unchanged before/after.
- [ ] Golden path: `get_guide` returns non-empty text with no daemon
      connection; `list_cameras`/`get_tags`/`where` all succeed against
      the isolated daemon.
- [ ] Paths: `draw_path` round-trips (same shape read back via `list`),
      a revision bump is observable, the deskewed `get_frame`'s rendered
      pixel at the polyline midpoint matches the expected drawn color
      within tolerance, and `clear_layer` removes it (confirmed absent on
      a subsequent `list`).
- [ ] Off-center mount_z: `mount_z_applied == False` and position
      unmoved before camera geometry is pushed; `mount_z_applied == True`,
      hypot displacement > 2 cm, per-axis abs=2.0 after; restored to the
      pre-registration position after `unregister_tag`.
- [ ] The displacement constants used here are imported from
      `test_camera_geometry_hardware.py`'s shared helper, not
      redeclared — grep-verified.
- [ ] Unplugging every qualifying camera skips this test cleanly with
      remediation text.

## Implementation Plan

**Approach**: Build the isolated-session fixture first (roots + private
daemon + pushed config, mirroring
`test_camera_geometry_hardware.py:204-227`'s proven pattern exactly, and
ticket 009's stdio client), then implement the three flows against it in
one test function (or three functions sharing one session-scoped
fixture, whichever keeps setup cost to one isolated daemon spin-up).

**Files to create/modify**:
- `tests/aprilcam/mcp/test_stdio_hardware.py` (new)
- Shared displacement-constant helper — extract from
  `test_camera_geometry_hardware.py` into a shared location if not
  already importable, so both modules reference one source (a small,
  additive refactor, not a rewrite of that file).

**Testing plan**:
- Requires a real camera (2, 3, or 5) attached; skips cleanly otherwise.
- **Verification command**: `uv run pytest tests/aprilcam/mcp/test_stdio_hardware.py -m "camera and integration" -v`

**Documentation updates**:
- None required; traceability filled in ticket 018's final sweep.
