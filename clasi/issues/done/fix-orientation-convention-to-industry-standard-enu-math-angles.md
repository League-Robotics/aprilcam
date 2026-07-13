---
status: done
---

# Fix orientation convention to industry-standard ENU "math angles"

## Context

The robot drives to the wrong edge: forward motion works but every
sideways/lateral component is inverted. Root cause is a **coordinate-frame
mismatch between reported positions and reported angles**, confirmed by
research and codebase mapping:

- **Industry standard** (ROS REP-103 / ENU "math angles"): right-handed,
  Z up, **yaw counter-clockwise positive, 0° along +X**, so
  `forward = (cos yaw, sin yaw)`. (NED/"compass" is the opposite: z-down,
  CW, 0°=north — explicitly *not* what we want.) Stakeholder chose **0° = +X
  (right/east), CCW positive**.
- **What the robot was written against** — [ROBOT_API_GUIDE.md:163-170](src/aprilcam/ROBOT_API_GUIDE.md#L163-L170)
  already documents `heading = tag.yaw; ax = rx + cos(heading); ay = ry + sin(heading)`.
  That is precisely the chosen convention. **The docs are already correct;
  the producer code is not.**
- **What the code actually does today** (the bug):
  - Reported `world_xy` is already **y-up, x-right, origin at A1** — the
    A1-centering step flips Y (`wy = origin_y - raw_wy`) in both
    [mcp_server.py:170](src/aprilcam/server/mcp_server.py#L170) and
    [camera_pipeline.py:661](src/aprilcam/daemon/camera_pipeline.py#L661).
  - But `orientation_yaw` is computed `atan2(-nx,-ny)` from **pixel** corners
    ([models.py:66](src/aprilcam/core/models.py#L66),[:102](src/aprilcam/core/models.py#L102)) →
    y-up but **0°=+Y** (90° off the chosen convention) and ignores camera rotation.
  - `heading_rad` is `atan2(wvy, wvx)` from **raw y-down** world velocity
    ([pipeline.py:216](src/aprilcam/core/pipeline.py#L216),[playfield.py:494](src/aprilcam/core/playfield.py#L494))
    → opposite Y-handedness from the reported position frame. **This is the inverted lateral.**

**Outcome:** make every reported angle/velocity consistent with the
already-correct reported position frame (x-right, y-up, origin A1), with
`yaw = atan2(Δy, Δx)`, 0°=+X, CCW — honoring the documented `(cos yaw, sin yaw)`
contract. No recalibration required.

## Approach: normalize reported angles to the y-up report frame

Keep the raw homography as-is (OpenCV-friendly y-down) — **no calibration
changes, no recalibration, no migration, no viewer changes**. Reported
positions are already correct and stay byte-for-byte identical. Only the
**angle/velocity producers** change so they live in the same y-up, 0°=+X
frame as the positions the robot consumes.

Centralize the convention in **one helper** so this class of bug can't recur
from scattered `atan2` calls. Add to [core/models.py](src/aprilcam/core/models.py)
(or a small `core/geometry.py`):

```python
def world_yaw(dx_raw: float, dy_raw: float) -> float:
    """Yaw of a raw-(y-down)-world direction in the reported ENU frame:
    x-right, y-up, 0°=+X, CCW positive."""
    return math.atan2(-dy_raw, dx_raw)
```

`forward = (cos yaw, sin yaw)` then holds by construction, matching the guide.

### Change sites (all reduce to `world_yaw(...)` / negate raw world-Y)

1. **`orientation_yaw` — compute in world space, 0°=+X** (also fixes the
   latent camera-rotation error where [models.py:66](src/aprilcam/core/models.py#L66)
   used pixel corners):
   - [models.py](src/aprilcam/core/models.py) `from_corners` & `update`: when
     `homography` is present, transform `center` and `top_mid` through it,
     `d = tw - cw`, `yaw = world_yaw(d[0], d[1])`. When absent (pixel-only/
     uncalibrated), fall back to pixel dir: `yaw = world_yaw(nx, ny)`
     (i.e. `atan2(-ny, nx)`). The homography is already passed in for `world_xy`.
   - [composite.py:135](src/aprilcam/camera/composite.py#L135): `d` is already
     raw-world → `yaw = world_yaw(d[0], d[1])`.

2. **`heading_rad` + reported `vel_world` — flip raw world-Y to y-up:**
   - [pipeline.py:212-216](src/aprilcam/core/pipeline.py#L212-L216): report
     `vel_world = (wvx, -wvy)` and `heading_rad = world_yaw(wvx, wvy)`
     (= `atan2(-wvy, wvx)`). `speed_world` unchanged.
   - [playfield.py:485-495](src/aprilcam/core/playfield.py#L485-L495): same.

3. **Docstrings/comments → state "ENU: x right, y up, yaw CCW from +X,
   forward=(cos,sin)":** [models.py:21-23](src/aprilcam/core/models.py#L21) &
   inline comments at :63-65/:99-101; [composite.py:134](src/aprilcam/camera/composite.py#L134);
   `get_tags`/`stream_tags` docstrings [mcp_server.py:2672](src/aprilcam/server/mcp_server.py#L2672),[:2748](src/aprilcam/server/mcp_server.py#L2748).
   ROBOT_API_GUIDE.md is already correct — leave its formula; optionally add an
   explicit "0°=+X, CCW" sentence.

### Verified to need NO change
- **Reported `world_xy`** and the A1-centering flips — already y-up ENU. Untouched.
- **`gripper_world_xy`** ([mcp_server.py:1041-1077](src/aprilcam/server/mcp_server.py#L1041-L1077)) —
  computes its offset in raw world then flips the *final point* together with
  position, so it is already self-consistent. Untouched.
- **Viewer** ([display.py](src/aprilcam/ui/display.py), web table): draws from
  pixel-space `top_dir_px`/pixel velocity and round-trips the *unchanged* y-up
  position frame via `origin_y - y`. No functional change.
- **Calibration storage/homography/`pixel_to_world`** — unchanged.

## Tests
- Existing [test_daemon_cli.py](tests/test_daemon_cli.py) /
  [test_client_models.py](tests/test_client_models.py) round-trip hardcoded
  values (don't exercise the formula) → unaffected; positions tests
  ([test_parallax_integration.py](tests/test_parallax_integration.py)) unaffected.
- **Add** a unit test pinning the convention: a tag whose top edge faces world
  +X → `yaw ≈ 0`; faces +Y (up) → `yaw ≈ +π/2`; and assert
  `(cos yaw, sin yaw)` points along the world Δ from center to top-mid. Cover
  both the homography and pixel-fallback paths in `AprilTag.from_corners`, and
  `map_tags_to_primary` in composite.py. Add a `heading_rad` case: world
  velocity toward +Y (up) → `heading ≈ +π/2`.

## Verification (end-to-end)
1. `pytest` — all green.
2. Live check with a calibrated camera + robot tag: `start_detection`, then
   `get_tags`; with the tag pointing right (+X) confirm `orientation_yaw ≈ 0`,
   pointing up confirm `≈ +90°`; push the tag toward +Y and confirm
   `heading_rad ≈ +90°` and `vel_world` y-component is **positive**.
3. Confirm `forward = (cos yaw, sin yaw)` lands ahead of the tag (drive a small
   commanded forward step; verify `world_xy` moves along +forward, and a
   commanded left step moves +90° CCW — lateral no longer inverted).
4. Per project convention, **bump version** in `pyproject.toml` after the change
   (`dotconfig version bump`) and run the suite once more.

## Notes
- On branch `sprint/011-...` (state: review); this is a focused correctness fix
  continuing the static-camera deskew work already on this branch.
- Optional future cleanup (NOT in this plan): flip the raw homography to y-up to
  eliminate the internal dual-frame entirely — defer; it forces recalibration or
  a load-time homography/static-marker migration and isn't needed to fix the bug.
