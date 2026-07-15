---
id: '002'
title: Decouple config reads from calibration reads in the loader and playfield marker-id
  lookup
status: open
use-cases: [SUC-004, SUC-005]
depends-on: ['001']
github-issue: ''
issue: split-config-from-state-into-fhs-xdg-roots.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Decouple config reads from calibration reads in the loader and playfield marker-id lookup

## Description

This is the one real code coupling this sprint must break:
`load_calibration_from_camera_dir(camera_dir, camera_config=None,
playfield_def=None)` (`calibration/calibration.py` 375-508) reads
`calibration.json` from `camera_dir` (state) AND — when `camera_config` is
not pre-supplied — overlays `config.json` from that *same* `camera_dir`
(line 453: `cfg = camera_config if camera_config is not None else
_load_config(camera_dir)`). The MCP path already supplies `camera_config`
and is unaffected by this ticket. Daemon-local callers passing a bare
`camera_dir` (wired in ticket 003) need the config read to resolve against
a different root.

Add a 4th optional parameter, `config_dir: str | Path | None = None`. When
`camera_config` is `None` and `config_dir` is given, resolve the config
read as `<config_dir>/<camera_dir.name>/config.json` (reconstructing the
per-camera slug subdirectory under the config root) instead of
`<camera_dir>/config.json`. When `config_dir` is omitted (`None`),
preserve today's exact sibling-read behavior — this is the backward-compat
guarantee that keeps every caller not yet updated (and every existing
test) green.

`calibrate_from_playfield_def`'s daemon-local write branch (~1348, `if not
skip_save:`) currently writes both `calibration.json` and `config.json` to
one `camera_dir`. Split it: `calibration.json` continues to write via
`save_calibration_to_camera_dir(..., state_dir)`; `config.json` writes via
`save_camera_config(config_dir, ...)` to the (new, separate) config root
parameter this function gains. Pick and document an explicit
write-ordering/failure-reporting behavior for the two writes (Open
Question 2 in `architecture-update.md` — recommend: write state first
since it's the higher-frequency, app-generated file; if the config write
then fails, log a warning rather than raising, since the state write — the
actually time-critical one for `get_tags` to work — already succeeded).
The MCP `skip_save=True` branch is unaffected (it already returns without
writing).

`core/playfield.py:_load_static_marker_ids(self, path: str)` (933) derives
`config_path = cal_path.parent / "config.json"` — a sibling-file
assumption on the *calibration file's* parent directory (not
`camera_dir` — `path` here is a path to the calibration/homography file
itself, called from both call sites — 617 and 825 — with such a path, e.g.
`str(found)`). Add an optional 2nd parameter, `config_dir: str | Path |
None = None`. When given, check
`Path(config_dir) / Path(path).parent.name / "config.json"` first (using
the calibration file's parent directory *name* as the camera slug);
fall back to the existing sibling lookup when `config_dir` is `None`.
Update both call sites (617, 825) to pass a config dir — this ticket wires
the `_load_static_marker_ids` signature and the two intra-`playfield.py`
call sites; wiring the *value* passed in from `Config` all the way through
the daemon boundary is ticket 003's job (this ticket may pass `None` from
those two call sites for now, deferring the actual threading to ticket 003,
OR do both in one pass if it's cleaner — implementer's discretion, but the
signature change and fallback behavior must land in this ticket regardless).

See `../architecture-update.md` §2-3 and Design Rationale ("loader
parameter is `config_dir`, not a boolean flag or separate function") for
full design detail.

## Acceptance Criteria

- [ ] `load_calibration_from_camera_dir` accepts a 4th optional
      `config_dir` parameter; omitting it reproduces byte-identical
      behavior to before this ticket (regression guard via existing
      tests).
- [ ] When `camera_config` is `None` and `config_dir` is provided, the
      config overlay (`device_name`, `resolution`, `settings`,
      `static_marker_ids`, `camera_position`) is read from
      `<config_dir>/<slug>/config.json`, not `<camera_dir>/config.json`.
- [ ] When `camera_config` is provided (MCP path), `config_dir` has no
      effect — the pre-loaded dict is used exactly as today.
- [ ] A calibration file present in `camera_dir` (state) with NO
      `config.json` alongside it, but a `config.json` present under
      `config_dir/<slug>/`, still yields a fully-populated calibration
      (config overlay fields present) when `config_dir` is passed.
- [ ] `calibrate_from_playfield_def`'s daemon-local write branch writes
      `calibration.json` to the state dir and `config.json` to a
      separately-parameterized config dir; the two writes' ordering and
      failure-reporting behavior is documented in the function's
      docstring.
- [ ] MCP `skip_save=True` calibration path continues to write neither
      file (unaffected).
- [ ] `_load_static_marker_ids` accepts an optional 2nd `config_dir`
      parameter; omitting it reproduces today's sibling-lookup behavior
      exactly.
- [ ] When `config_dir` is provided and a matching `config.json` exists
      under `<config_dir>/<slug>/`, `static_marker_ids` from that file
      takes priority over the calibration-file fallback, matching the
      existing "config wins" contract.
- [ ] `test_config_calibration_split.py`'s canonical fixture
      (`arducam-ov9782-usb-camera` holding both files in one dir) continues
      to pass unmodified as the backward-compat (`config_dir=None`) case.
- [ ] Full `uv run pytest` green.

## Testing

- **Existing tests to run**: `tests/test_config_calibration_split.py`,
  `tests/test_calibrate_from_def.py`, `tests/test_sprint_012_integration.py`,
  `tests/test_calibration_parallax.py`, `tests/test_parallax_integration.py`,
  `tests/test_open_camera_rehydrate.py`,
  `tests/test_calibration_geometry_persist.py`, full `uv run pytest`.
- **New tests to write**:
  - `load_calibration_from_camera_dir` with `calibration.json` and
    `config.json` in two different tmp dirs, `config_dir` passed
    explicitly — asserts the overlay fields come from the config-root
    file, geometry fields from the state-dir file.
  - `load_calibration_from_camera_dir(config_dir=None)` regression case —
    identical output to current behavior on the shared-dir fixture.
  - `camera_config` dict short-circuit still bypasses `config_dir`
    entirely (pass a `config_dir` pointing at a directory with a
    *different* config.json content and confirm it's ignored when
    `camera_config` is also passed).
  - `calibrate_from_playfield_def` daemon-local branch: after calling with
    distinct state/config dirs, assert `calibration.json` exists only
    under the state dir and `config.json` exists only under the config
    dir; assert `skip_save=True` writes neither.
  - `_load_static_marker_ids` with an explicit `config_dir` pointing at a
    different location than the calibration file's sibling; with
    `config_dir=None` (regression); with both present but a different
    `static_marker_ids` value in each (config-dir version should win).
- **Verification command**: `uv run pytest`
