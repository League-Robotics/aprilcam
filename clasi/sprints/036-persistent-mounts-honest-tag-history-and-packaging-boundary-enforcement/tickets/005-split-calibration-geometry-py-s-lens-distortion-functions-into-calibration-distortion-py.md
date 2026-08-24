---
id: '005'
title: Split calibration/geometry.py's lens-distortion functions into calibration/distortion.py
status: open
use-cases: [SUC-003]
depends-on: ["004"]
github-issue: ''
issue: opencv-is-a-base-dependency-contradicting-the-daemon-only-contract.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Split calibration/geometry.py's lens-distortion functions into calibration/distortion.py

## Description

Ticket 004 moved `opencv-contrib-python` to the `daemon` extra, but
`src/aprilcam/calibration/geometry.py` has its own top-level
`import cv2 as cv` — needed **only** by two functions,
`undistort_point`/`distort_point` (the lens-distortion round trip,
`cv2.undistortPoints`). Every other function in `geometry.py`
(`apply_homography`/`invert_homography`/`pixel_to_world`/
`world_to_pixel`, `deskewed_output_size`/`world_to_deskewed_pixel`,
`project_tag_to_field_plane`/`apply_mount_offset`/`correct_tag_pose`,
`solve_camera_geometry`) is pure NumPy/math with zero `cv2` usage —
confirmed by reading the module during sprint planning.

`client/renderer.py` (`world_to_deskewed_pixel`) and `cli/locate.py`
(`solve_camera_geometry`) both import directly from `geometry.py` for
functions that never call `cv2` — but because the whole module has a
top-level `cv2` import, importing either of those files transitively
requires `cv2`. Before ticket 004, this was invisible because `cv2`
was a base dependency anyway. After ticket 004, this is a **real
regression**: `client/renderer.py` backs `aprilcam view` and MCP
annotation rendering, both meant to work in a base-only install per
the project overview — `import aprilcam.client.renderer` would now
raise `ModuleNotFoundError` in a fresh base-only venv.

The fix: move `undistort_point`/`distort_point` (and their two tuning
constants, `_DISTORT_POINT_MAX_ITERATIONS`/`_DISTORT_POINT_TOLERANCE_PX`)
into a new sibling module, `src/aprilcam/calibration/distortion.py`.
`geometry.py` loses its `cv2` import entirely.
`src/aprilcam/calibration/calibration.py` (the only consumer of
`undistort_point`/`distort_point` — confirmed by grep during sprint
planning: `calibration.py` lines ~387, ~395, ~1328) updates its calls
from `geometry.undistort_point(...)` to `distortion.undistort_point(...)`
(and likewise for `distort_point`), importing the new module alongside
`geometry`. See this sprint's `design/` overlay
(`clasi/sprints/036-.../design/calibration-DESIGN.diff.md`) for the
full Design Rationale (Decision/Context/Alternatives/Consequences) —
read it before starting; it explains why a module split was chosen
over a lazy `cv2` import inside `geometry.py` itself.

**Depends on ticket 004**: the new packaging-test coverage below only
proves something meaningful once `opencv-contrib-python` is actually
out of `[project.dependencies]` (ticket 004's change) — verifying this
ticket's fix against a venv that still has `cv2` in base would pass
trivially either way.

## Acceptance Criteria

- [ ] New file `src/aprilcam/calibration/distortion.py` holds
      `undistort_point`, `distort_point`, and their two tuning
      constants, with the `import cv2 as cv` that used to live in
      `geometry.py`.
- [ ] `src/aprilcam/calibration/geometry.py` no longer imports `cv2`
      anywhere — verify with `grep -n cv2
      src/aprilcam/calibration/geometry.py` returning nothing.
- [ ] `src/aprilcam/calibration/calibration.py` imports the new
      `distortion` module alongside `geometry` and calls
      `distortion.undistort_point`/`distortion.distort_point` at its
      three existing call sites; behavior is otherwise unchanged
      (this is a pure move, not a rewrite of the distortion math).
- [ ] `tests/aprilcam/calibration/test_geometry.py`'s
      `undistort_point`/`distort_point` tests (the "undistort_point /
      distort_point (sprint 030 ticket 001)" section) move to a new
      `tests/aprilcam/calibration/test_distortion.py`, importing from
      `aprilcam.calibration.distortion` instead of `.geometry`. The
      remaining tests in `test_geometry.py` (homography, deskew
      geometry, mount correction, camera-geometry solver) are
      unaffected and stay where they are.
  - [ ] Check `tests/aprilcam/config/test_loader.py` and
        `tests/image_matrix/test_geometry.py` (both import
        `calibration.geometry` per a repo-wide grep during sprint
        planning) for any use of `undistort_point`/`distort_point` —
        expected to be none (they likely use unrelated `geometry`
        functions), but confirm rather than assume; update their
        imports only if they do.
- [ ] New packaging-test coverage in
      `tests/packaging/test_installed_package.py`: assert
      `import aprilcam.client.renderer` succeeds (subprocess `-c`
      check, same pattern as the existing `import aprilcam.client`/
      `import cv2` checks) in the fresh **base-only** venv. Optionally
      also assert `import aprilcam.cli.locate` succeeds there (a bonus
      fix, per `cli/DESIGN.md`'s resolved §3/§6 — not required, but
      cheap given the fixture already exists).
- [ ] `docs/design/design.md` §9's item 6 and `cli/DESIGN.md` §3/§6
      already describe this fix in this sprint's `design/` overlay —
      no further doc edits needed beyond what ticket 007/008 (mount
      persistence docs) or this ticket's own code comments require.

## Testing

- **Existing tests to run**:
  `uv run pytest tests/aprilcam/calibration/ tests/packaging/test_installed_package.py tests/test_no_cv2_in_client_modules.py -q`
  — note `test_installed_package.py` builds a real wheel and fresh
  venv; it is slow, not a quick unit-test run.
- **New tests to write**: `tests/aprilcam/calibration/test_distortion.py`
  (moved from `test_geometry.py`, imports updated); the new
  `import aprilcam.client.renderer` (and optionally `cli.locate`)
  assertion in `test_installed_package.py`.
- **Verification command**:
  `uv run pytest tests/aprilcam/calibration/ tests/packaging/test_installed_package.py tests/test_no_cv2_in_client_modules.py -q`
