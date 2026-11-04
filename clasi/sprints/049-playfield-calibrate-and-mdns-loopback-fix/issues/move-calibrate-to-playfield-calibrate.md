---
status: in-progress
sprint: 049
tickets:
- 049-001
- 049-002
---

# Move `camera calibrate` to `playfield calibrate`

## Description

Stakeholder request (2026-11-04). Today `aprilcam camera` has three
calibration-like subcommands: `intrinsics` (lens), `flatfield`, and
`calibrate`. The first two describe the camera itself. `calibrate` solves
the camera's pose against the playfield's surveyed tags, which makes it a
playfield operation. It belongs under `aprilcam playfield`.

The stakeholder isn't attached to the word "calibrate"; the CLI's own
listing calls the result "pose" (`calibrated (pose, full|reduced)`). Keep
`calibrate` unless sprint planning finds a clearly better name.

### Wanted

- `aprilcam playfield calibrate [CAMERA]`: CAMERA accepts a slug, a
  number, or a host-qualified `HOST-N` (e.g. `angst-4`), the same as
  `camera image` and the other camera-addressed commands. `angst-4`
  calibrates the playfield seen by camera 4 on angst, from any machine.
- When CAMERA is omitted, resolve it the way `playfield image`/`view`
  already do (`resolve_playfield_camera` in `cli/_common.py`): exactly one
  linked, present camera is used; zero or several is an error that lists
  the candidates. Decide in planning whether CAMERA is positional (as the
  stakeholder typed it) or `--camera` (as `playfield image` uses).
- The result stays stored per camera (`cameras/<slug>/calibration.json`).
  A playfield can have more than one camera (main-playfield links the
  OV9281 and OV9782), and each has its own pose.
- `camera calibrate`: keep it for one release as a deprecated alias that
  prints a pointer to `playfield calibrate`, then remove it.
- Update every place that points users at `camera calibrate`, e.g. the
  `NotCalibrated` hint in `cli/playfield.py` ("calibrate first with
  'aprilcam camera calibrate …'"), the packaged guides, `cli/DESIGN.md`,
  the wikis, and `deploy/README.md`.
- MCP: the `calibrate` tool is already camera-addressed and accepts
  `HOST-N`. Check that its description and `get_guide` text describe it as
  a playfield/pose operation.

### Already works (checked 2026-11-04, v0.20261101.3)

`HOST-N` addressing from sprint 048 is already wired into
`camera calibrate`, `camera intrinsics`, `camera flatfield` and
`camera locate` (`connect_for_camera` / `resolve_camera`).
`resolve_camera('angst-4')` resolves to angst's daemon, camera 4. The new
`playfield calibrate` must keep this. Running intrinsics against a remote
camera (the GUI on the client, frames from the remote daemon) should get
an end-to-end test against angst.
