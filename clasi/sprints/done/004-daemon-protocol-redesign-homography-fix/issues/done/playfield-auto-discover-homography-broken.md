---
status: done
sprint: '004'
tickets:
- 004-001
---

# Fix Playfield._auto_discover_homography always returning None

`Playfield._auto_discover_homography()` calls `discover_homography()` with no
arguments, but the real signature requires `(device_name, width, height, data_dir)`.
The resulting `TypeError` is silently caught, so `self._homography` is always `None`
after auto-discovery.

## Impact

`tag.wx` and `tag.wy` are always `None` when using the `Playfield` Python API,
even on a field that has been calibrated and has a valid homography file on disk.

## Location

`src/aprilcam/core/playfield.py`, method `_auto_discover_homography`, ~line 479.

## Fix

`_auto_discover_homography` needs the camera's device name and resolution to call
`discover_homography(device_name, width, height)`. The camera isn't open yet at
`Playfield.__init__` time, so the options are:

1. Open the camera briefly inside `_auto_discover_homography` to read its name and
   resolution, then close it before the pipeline opens it again.
2. Accept `device_name` and resolution as optional constructor parameters on
   `Playfield`, falling back to opening the camera when not provided.
3. Defer homography discovery until `start()` is called (camera is open by then).
