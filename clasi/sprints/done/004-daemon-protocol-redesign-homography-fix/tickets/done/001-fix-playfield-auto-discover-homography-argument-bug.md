---
id: '001'
title: Fix Playfield._auto_discover_homography argument bug
status: done
use-cases:
  - SUC-008
depends-on: []
github-issue: ''
issue: playfield-auto-discover-homography-broken.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Fix Playfield._auto_discover_homography argument bug

## Description

`Playfield._auto_discover_homography()` calls `discover_homography()` with no
arguments. The real signature is `discover_homography(device_name, width, height,
data_dir)`. The resulting `TypeError` is silently caught, so `self._homography` is
always `None` even when a valid calibration file exists on disk.

Fix: defer `_auto_discover_homography()` from `__init__` to `start()` time, where
the camera is already open and `device_name` and `(width, height)` are available.
Pass the correct arguments when calling `discover_homography()`.

**File**: `src/aprilcam/core/playfield.py`, method `_auto_discover_homography`,
~line 478.

## Acceptance Criteria

- [ ] `_auto_discover_homography()` is called inside `start()` (not `__init__`).
- [ ] `discover_homography(device_name, width, height, data_dir=...)` is called with
      the correct arguments obtained from the open camera.
- [ ] `self._homography` is a 3x3 numpy array (not None) when a calibration file
      exists for the camera.
- [ ] `tag.wx` and `tag.wy` are non-None floats on a calibrated field.
- [ ] No regressions in existing playfield tests.

## Implementation Plan

### Approach

1. Remove or no-op the `_auto_discover_homography()` call in `Playfield.__init__`.
2. In `Playfield.start()`, after opening the camera and reading its device name and
   resolution (width, height), call `self._auto_discover_homography(device_name, width, height)`.
3. Update `_auto_discover_homography` signature to accept `device_name: str`,
   `width: int`, `height: int`, and forward them to `discover_homography`.

### Files to Modify

- `src/aprilcam/core/playfield.py`
  - Move homography discovery call from `__init__` to `start()`.
  - Update `_auto_discover_homography` to accept and pass through device_name, width, height.
  - Determine `data_dir` from `self._config.data_dir` or equivalent config path.

### Testing Plan

- Write `tests/test_playfield_homography.py`:
  - Create a temporary directory with a mock `calibration.json` or
    `homography-<slug>.json` file containing a known 3x3 matrix.
  - Construct a `Playfield` instance pointing at that data dir.
  - After calling `start()` (mocked camera), assert `playfield._homography` is a
    3x3 numpy array matching the mock data.
  - Assert that `_auto_discover_homography` when called with mismatched device_name
    returns None gracefully.
- Run `uv run pytest` to verify no regressions.
