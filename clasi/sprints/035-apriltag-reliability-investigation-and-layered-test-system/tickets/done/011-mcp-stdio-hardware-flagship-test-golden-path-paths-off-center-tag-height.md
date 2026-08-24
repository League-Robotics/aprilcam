---
id: '011'
title: 'MCP stdio hardware flagship test: golden path, paths, off-center tag height'
status: done
use-cases:
- UC-22
- UC-12
depends-on:
- 009
- '010'
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
      **Not executed against real hardware.** The session (gate, setup,
      golden path, paths, off-center flows) is fully written and
      structurally verified to run over one continuous
      `stdio_client`/`ClientSession` connection, but every real run in
      this environment (an agent session with no macOS camera TCC
      grant) skips at the pre-flight gate before the golden
      path/paths/off-center bodies ever execute — see Completion Notes.
      The parameterization sub-property genuinely is verified: two
      cases are collected (`arducam-ov9281-usb-camera`,
      `arducam-ov9782-usb-camera` — camera 2/`hd-usb-camera` does not
      currently qualify against `main-playfield` on this rig, matching
      `resolve_all_hardware_camera_identities`'s own documented
      reasoning from ticket 010), and each skips independently with its
      own message.
- [ ] Every write in this flow (config push, calibration, mount
      registration) lands under the isolated
      `APRILCAM_CONFIG_DIR`/`APRILCAM_STATE_DIR` roots — spot-checked
      that the real `~/.config/aprilcam`/`~/.local/share/aprilcam` are
      unchanged before/after.
      **Isolation mechanism verified; writes-under-isolation not yet
      exercised.** `daemon_env`'s `socket_dir` isolation check and the
      `real_system_untouched_guard` fixture (real file
      mtime+sha256 fingerprints, before/after) both ran cleanly for both
      parameterized cases. Since every real run skips before any
      `set_playfield`/`set_camera_config`/`calibrate`/`register_tag`
      call is ever made, this has only proven "no writes happened and
      none were expected," not "a real push landed only in the isolated
      root" — that half needs an operator run.
- [ ] Golden path: `get_guide` returns non-empty text with no daemon
      connection; `list_cameras`/`get_tags`/`where` all succeed against
      the isolated daemon.
      **Not executed against real hardware** — see Completion Notes.
- [ ] Paths: `draw_path` round-trips (same shape read back via `list`),
      a revision bump is observable, the deskewed `get_frame`'s rendered
      pixel at the polyline midpoint matches the expected drawn color
      within tolerance, and `clear_layer` removes it (confirmed absent on
      a subsequent `list`).
      **Not executed against real hardware** — see Completion Notes.
- [ ] Off-center mount_z: `mount_z_applied == False` and position
      unmoved before camera geometry is pushed; `mount_z_applied == True`,
      hypot displacement > 2 cm, per-axis abs=2.0 after; restored to the
      pre-registration position after `unregister_tag`.
      **Not executed against real hardware** — see Completion Notes. Uses
      ArUco 2 rather than ArUco 1 for the off-nadir marker (~47.6 cm
      from this rig's real nadir, not ~70 cm) — see the test module's
      own `MOUNT_Z_TEST_ARUCO_ID` docstring for why: ArUco 1 is only
      7/12 reliable on camera 5 per this sprint's own live rig facts,
      while ArUco 2 is 12/12 on both cameras 3 and 5, and still gives a
      clean, above-noise ~7.4 cm expected displacement at `mount_z=20`.
- [x] The displacement constants used here are imported from
      `test_camera_geometry_hardware.py`'s shared helper, not
      redeclared — grep-verified.
      Imported directly from `tests/aprilcam/daemon/mount_z_math.py`
      (`REAL_NADIR_X_CM`, `REAL_NADIR_Y_CM`, `REAL_HEIGHT_CM`,
      `expected_parallax_corrected_position`) — the same shared helper
      `test_camera_geometry_hardware.py` itself now imports from.
      `grep -n "REAL_NADIR_X_CM\s*=" tests/aprilcam/mcp/test_stdio_hardware.py`
      matches only the docstring prose describing this check, no
      assignment.
- [x] Unplugging every qualifying camera skips this test cleanly with
      remediation text.
      Genuinely verified this session: both parameterized cases
      (`arducam-ov9281-usb-camera`, `arducam-ov9782-usb-camera`) hit the
      real `get_frame` failure this environment's lack of a camera TCC
      grant actually produces, and both report a clean `SKIPPED` with
      full, actionable remediation text (not a hard failure, not an
      opaque `anyio` internal error — see Completion Notes for the exact
      output and the fix that got it there).

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

## Completion Notes

**What the test covers.** `tests/aprilcam/mcp/test_stdio_hardware.py`
drives one continuous real `aprilcam mcp` stdio session
(`mcp.client.stdio.stdio_client` + `mcp.ClientSession`, matching ticket
009's client) against a private, isolated `aprilcam daemon` subprocess
(short-`/tmp` `XDG_RUNTIME_DIR`, isolated `APRILCAM_CONFIG_DIR`/
`APRILCAM_STATE_DIR`, matching `test_camera_geometry_hardware.py:204-227`'s
`cli_env` pattern), parameterized per camera via ticket 010's
`resolve_all_hardware_camera_identities(playfield="main-playfield")`
(dynamic `pytest_generate_tests`, one parameterized case per qualifying
camera — currently `arducam-ov9281-usb-camera` and
`arducam-ov9782-usb-camera` on this rig). Within that one session:
a pre-flight gate (`list_cameras` + a raw, non-deskewed `get_frame`,
never enumeration alone); setup (`set_playfield`/`set_camera_config`
with no `camera_position`/`calibrate`); the golden path (`get_guide` ->
`list_cameras` -> `get_tags` -> `where`); paths (`draw_path` -> round
trip + revision check via `list_annotations` -> a deskewed `get_frame`
whose bytes are decoded and re-rendered in-process with
`client.renderer.Renderer` (`get_frame`'s own MCP tool never exposes
`px_per_cm`/`world_extent`/`annotations`, so this reconstructs
`px_per_cm` exactly from the decoded JPEG's own pixel width and the
playfield's `width_cm`, per `calibration/deskew.py`'s own "exact on the
width axis" guarantee) -> a pixel-neighborhood color check at the
polyline's midpoint -> `clear_layer` -> confirmed gone); and off-center
mount_z (`register_tag` `mount_z=20` on ArUco 2 -> `mount_z_applied ==
False` and unmoved -> `set_camera_config` with this rig's real,
imported `camera_position` -> `mount_z_applied == True`, hypot
displacement > 2 cm, per-axis `abs=2.0` of the shared helper's predicted
position -> `unregister_tag` -> restored). A
`real_system_untouched_guard` fixture fingerprints (mtime + sha256) the
real system's per-camera `config.json`/`calibration.json` and
`main-playfield.json` before and after every case.

**Exactly which assertions have never run against real hardware.**
None of them. Every assertion past the pre-flight gate — the entire
setup, golden path, paths, and off-center bodies — is written but has
never executed in this environment, because this agent session's
private daemon subprocess inherits no macOS camera TCC grant (see
`docs/knowledge/daemon-camera-grant-and-detection-priming.md`) and the
gate correctly stops the session there, every time, for every camera.
What genuinely *has* run and been observed passing: the gate itself
(both failure branches — `list_cameras` succeeding while a raw
`get_frame` fails, exactly the documented failure mode), the dynamic
per-camera parameterization, the `daemon_env` isolation-skip check, the
`real_system_untouched_guard` fingerprint fixture (trivially, since no
writes occur before the skip), and the daemon subprocess's own clean
start/stop lifecycle (`ps aux` after each run shows no orphaned
`aprilcam.daemon`/`aprilcam mcp` process beyond the pre-existing,
untouched production daemon).

**A structural bug the gate uncovered and fixed, worth flagging for
whoever runs this next.** The first working draft's `pytest.skip()`
calls, made from inside the nested `stdio_client`/`ClientSession`
`async with` bodies, were silently swallowed by `anyio`'s TaskGroup
exception wrapping — every skip surfaced to pytest as an opaque
`SKIPPED ... anyio/_backends/_asyncio.py:783: unhandled errors in a
TaskGroup`, with no remediation text at all (would have failed this
ticket's own "skips cleanly with remediation text" acceptance
criterion). Fixed by extracting the whole session body into
`_run_flagship_session` and having the test function catch/store/
re-raise around it from *outside* the task-group-guarded scope — see
that function's own docstring. This applies to real assertion failures
inside the session too, not just skips, so it also protects a future
real hardware run from the same opaque-wrapping failure mode.

**Real skip output** (`uv run pytest tests/aprilcam/mcp/test_stdio_hardware.py -m "camera and integration" -rs -v`):

```
tests/aprilcam/mcp/test_stdio_hardware.py::test_mcp_stdio_hardware_flagship_session[arducam-ov9281-usb-camera] SKIPPED
tests/aprilcam/mcp/test_stdio_hardware.py::test_mcp_stdio_hardware_flagship_session[arducam-ov9782-usb-camera] SKIPPED

SKIPPED [1] tests/aprilcam/mcp/test_stdio_hardware.py:394: 'arducam-ov9281-usb-camera' qualifies on this machine's real system state but a raw get_frame over this isolated MCP session failed: Error executing tool get_frame: daemon at 'localhost' did not answer: failed to open camera at index 1. Expected when the process that launched this private daemon has no camera TCC grant (an agent session -- see docs/knowledge/daemon-camera-grant-and-detection-priming.md) or the device is genuinely busy elsewhere; re-run from an operator Terminal session (this sprint's test-hardware.sh, ticket 035-017) to exercise this camera for real.
SKIPPED [1] tests/aprilcam/mcp/test_stdio_hardware.py:394: 'arducam-ov9782-usb-camera' qualifies on this machine's real system state but a raw get_frame over this isolated MCP session failed: Error executing tool get_frame: daemon at 'localhost' did not answer: failed to open camera at index 3. Expected when the process that launched this private daemon has no camera TCC grant (an agent session -- see docs/knowledge/daemon-camera-grant-and-detection-priming.md) or the device is genuinely busy elsewhere; re-run from an operator Terminal session (this sprint's test-hardware.sh, ticket 035-017) to exercise this camera for real.
2 skipped in 11.96s
```

**What the operator must run to verify it for real.** From a Terminal
session (never from an agent — see this project's own hard rule on
never starting a daemon outside an isolated test fixture):
`uv run pytest tests/aprilcam/mcp/test_stdio_hardware.py -m "camera and integration" -v -s`
(this sprint's `test-hardware.sh`, ticket 035-017, once wired). A
Terminal-launched pytest process's own private daemon subprocess
inherits Terminal's camera grant, so the gate should pass and the full
session — setup, golden path, paths (including the rendered-pixel
check), and off-center mount_z/parallax — should genuinely execute for
the first time. If a real run instead fails inside the session body,
the exception-shielding fix above should still report the real
assertion text cleanly rather than an opaque `anyio` wrapper — if it
does not, that shielding has a gap worth its own follow-up.
