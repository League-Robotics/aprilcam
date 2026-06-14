---
id: '005'
title: open_camera auto-rehydrate PlayfieldEntry; wire calibrate_playfield and CLI
  to shared helper
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
depends-on:
- '001'
- '002'
- '003'
- '004'
github-issue: ''
issue: ''
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# open_camera auto-rehydrate PlayfieldEntry; wire calibrate_playfield and CLI to shared helper

## Description

Wires together the foundation from tickets 001-004 into the user-facing MCP and
CLI behaviors:

1. **`_handle_open_camera` rehydration** in `mcp_server.py` — after the daemon
   open call, attempt to auto-reconstruct a `PlayfieldEntry` from disk.

2. **`calibrate_playfield` MCP tool** — delegate to `calibrate_from_playfield_def`;
   change `width` and `height` from required to optional (default `None`).

3. **`aprilcam calibrate` CLI** — replace per-camera `calibrate_single` call with
   `calibrate_from_playfield_def`; remove the dimension-defaulting block.

### 1. `_handle_open_camera` rehydration

After the existing block that sets up `_cam_info` and writes `paths.json`, add:

```python
# --- Auto-rehydrate playfield entry from disk ---
try:
    camera_dir = Path(info.get("camera_dir", ""))
    if camera_dir:
        from aprilcam.camera.camera_config import load_camera_config
        from aprilcam.calibration.calibration import load_calibration_from_camera_dir

        cam_cfg = load_camera_config(camera_dir)
        if cam_cfg and "playfield" in cam_cfg:
            pf_slug = cam_cfg["playfield"]
            try:
                pf_def = playfield_def_registry.get(pf_slug)
            except KeyError:
                pf_def = None

            if pf_def is not None:
                cal = load_calibration_from_camera_dir(camera_dir, cam_cfg, pf_def)
                if cal is not None and cal.homography is not None:
                    # Guard: don't overwrite an existing PlayfieldEntry for this camera
                    existing_pid = playfield_registry.find_by_camera(handle)
                    if existing_pid is None:
                        pf_entry = PlayfieldEntry(
                            playfield_id=f"pf_{handle}",
                            camera_id=handle,
                            playfield=Playfield(detect_inverted=True, proc_width=0),
                            field_spec=FieldSpec(pf_def.width_cm, pf_def.height_cm, "cm"),
                            homography=cal.homography,
                            tag1_origin_cm=None,  # center-origin def; A1 IS (0,0)
                        )
                        # Inject polygon from H-inverse so deskew works
                        from aprilcam.calibration.geometry import corner_pixels_from_homography
                        poly = corner_pixels_from_homography(
                            cal.homography, pf_def.width_cm, pf_def.height_cm
                        )
                        pf_entry.playfield._poly = poly
                        playfield_registry.register(pf_entry)
                        result_extra = {
                            "playfield_id": f"pf_{handle}",
                            "playfield_name": pf_slug,
                        }
                        if getattr(cal, "calibration_stale", False):
                            result_extra["calibration_stale"] = True
                        # Merge into return value
                        return {**{"camera_id": handle, "cam_name": cam_name}, **result_extra}
except Exception as _rh_exc:
    logging.getLogger("aprilcam").warning("Playfield rehydration failed: %s", _rh_exc)
```

Return value: when rehydration succeeds, `open_camera` returns `camera_id`,
`cam_name`, `playfield_id`, `playfield_name`, and optionally
`calibration_stale: true`.

Note on `tag1_origin_cm`: the def uses center origin (A1 = 0,0), so
`_get_playfield_origin` must return `(0, 0)` for a center-origin def. Set
`tag1_origin_cm = (0.0, 0.0)` explicitly so the origin is not computed from the
field spec (which would give `(width/2, height/2)` = wrong for center origin).

### 2. `calibrate_playfield` MCP tool changes

- Change `width: float` and `height: float` to `width: Optional[float] = None`
  and `height: Optional[float] = None`.
- Replace the inner calibration block with a call to `calibrate_from_playfield_def`:

```python
camera_id = entry.camera_id
camera_dir_str = _cam_info.get(camera_id, {}).get("camera_dir", "")
if not camera_dir_str:
    return error("No camera_dir for camera_id")

camera_dir = Path(camera_dir_str)
camera_slug = camera_dir.name  # slug = dir name

try:
    from aprilcam.calibration.calibration import (
        calibrate_from_playfield_def, PlayfieldConfigError
    )
    cap = DaemonCapture(_ensure_daemon_client(), camera_id)
    cal = calibrate_from_playfield_def(
        cap=cap,
        camera_dir=camera_dir,
        camera_slug=camera_slug,
        playfield_def_registry=playfield_def_registry,
        camera_position=CameraPosition(
            x_offset=camera_x_offset_cm,
            y_offset=camera_y_offset_cm,
            height=camera_height_cm,
        ),
    )
except PlayfieldConfigError as exc:
    return error(str(exc))
```

After successful calibration, update the `PlayfieldEntry` with the new homography
and `field_spec`. Remove the `pixel_corners` / `calibrate_from_corners` block.

Note: the `playfield_id` parameter to `calibrate_playfield` currently identifies
a `PlayfieldEntry` in the runtime registry. This will still be the case after the
refactor — the entry is looked up, updated, and the response is unchanged.

### 3. `aprilcam calibrate` CLI changes

Replace the per-camera calibration block inside the `else` branch:

```python
for idx, label in camera_indices:
    cam_name, camera_dir_str = dc.open_camera(idx)
    _warmup_capture(dc, cam_name)
    camera_dir = cameras_dir / cam_name
    camera_slug = cam_name

    cap = _DaemonCapture(dc, cam_name)
    try:
        from aprilcam.calibration.calibration import (
            calibrate_from_playfield_def, PlayfieldConfigError
        )
        cal = calibrate_from_playfield_def(
            cap=cap,
            camera_dir=camera_dir,
            camera_slug=camera_slug,
            playfield_def_registry=playfield_def_registry,
            num_frames=args.frames,
        )
    except PlayfieldConfigError as exc:
        print(f"  ERROR: {exc}")
        print()
        continue
    ...
```

The CLI must load `playfield_def_registry` at startup (same as the MCP server):
```python
from aprilcam.core.playfield_def import PlayfieldDefinitionRegistry
playfield_def_registry = PlayfieldDefinitionRegistry()
playfield_def_registry.load_all(config.playfields_dir)
```

Remove the `field_width` / `field_height` default-loading block that reads from
existing `calibration.json` files. The `--width` and `--height` args remain in
the parser but are superseded by the def.

The `--joint` path for `calibrate_secondary` does NOT change this sprint —
secondary calibration uses the primary's homography, not a def. Leave it as-is.

## Acceptance Criteria

- [x] `open_camera` on a camera with matching `config.json` + `calibration.json`
      returns `playfield_id` and `playfield_name` in its response.
- [x] `open_camera` on a camera with mismatched provenance returns
      `calibration_stale: true` alongside the playfield fields.
- [x] `open_camera` on a camera with no `config.json` succeeds (opens camera)
      but returns no `playfield_id` field.
- [x] `open_camera` rehydration does NOT overwrite a `PlayfieldEntry` already
      created by a prior `create_playfield` call for the same camera.
- [x] `calibrate_playfield` MCP tool on a camera with `config.json` succeeds and
      returns dims from the def (134.3 × 89.3).
- [x] `calibrate_playfield` MCP tool on a camera without `config.json` returns
      `{"error": "Camera '...' has no playfield configured..."}`.
- [x] `aprilcam calibrate` on a camera with `config.json` succeeds (with live
      hardware; end-to-end test is manual per the issue Verification section).
- [x] `aprilcam calibrate` on a camera without `config.json` prints the
      `PlayfieldConfigError` guidance and exits cleanly.
- [x] Both MCP `calibrate_playfield` and `aprilcam calibrate` call the exact same
      `calibrate_from_playfield_def` function (confirmed by import trace in a unit
      test or grep).
- [x] `uv run pytest` passes.

## Implementation Plan

### Files to modify

- `src/aprilcam/server/mcp_server.py`
  - `_handle_open_camera`: add rehydration block after `_cam_info` setup.
  - `calibrate_playfield` tool: change param types; replace inner calibration
    block with `calibrate_from_playfield_def` call.

- `src/aprilcam/cli/calibrate_cli.py`
  - Add `playfield_def_registry` load at top of `main()`.
  - Replace per-camera `calibrate_single` call with `calibrate_from_playfield_def`.
  - Remove dimension-defaulting block.
  - Leave `--joint` path unchanged.

### Testing plan

Extend `tests/test_mcp_path_tools.py` or add `tests/test_open_camera_rehydrate.py`:
- `test_open_camera_rehydrate_builds_playfield_entry(tmp_path, monkeypatch)` —
  write fixture `config.json`, `calibration.json` with matching provenance;
  monkeypatch the daemon client and `registry.open`; call `_handle_open_camera`;
  assert `playfield_registry.find_by_camera(handle)` is set and result has
  `playfield_id`.
- `test_open_camera_stale_calibration(tmp_path, monkeypatch)` — mismatched
  provenance → `calibration_stale: true` in result.
- `test_open_camera_no_config(tmp_path, monkeypatch)` — no `config.json` → no
  `playfield_id` in result; no error.
- `test_calibrate_mcp_no_config_returns_error(monkeypatch)` — no `config.json`
  → `{"error": "...no playfield configured..."}` returned.

### Documentation updates

None for this ticket. Updated in ticket 007.
