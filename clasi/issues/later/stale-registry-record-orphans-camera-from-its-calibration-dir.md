---
status: pending
---

# Stale registry record orphans a re-plugged camera from its calibration dir (`<slug>-<enum>` suffix)

## Symptom

The daemon serves a camera as `arducam-ov9782-usb-camera-15`, but its
calibration lives at `cameras/arducam-ov9782-usb-camera/` (no `-15`).
The live pipeline reads calibration from
`<cameras_dir>/<cam_name>/calibration.json`
([daemon/camera_pipeline.py:271], [daemon/grpc_server.py:813]), so with
`cam_name = ...-15` it finds nothing → `get_calibration` empty,
`homography = None`, `world_xy = (0,0)`. `reload_calibration()` returns
`None` and changes nothing because there is no file at the suffixed path.

## Confirmed root cause

`registry.json` holds **two records for the same physical Arducam
OV9782** (same `name`, `vid: 3141`, same `pid`), differing only by USB
`location` — so they get different `unique_id`s and are treated as two
devices:

| unique_id (loc)        | dir                             | enum | last_seen  |
|------------------------|---------------------------------|------|------------|
| `...1000c456366`       | `arducam-ov9782-usb-camera`     | 3    | 2026-07-19 (STALE) |
| `...1400c456366`       | `arducam-ov9782-usb-camera-15`  | 15   | 2026-07-29 (live)  |

The Jul-19 record is stale (device gone) but still "owns" the bare-slug
dir. When the camera re-enumerated today at a new USB location it became
a **new `unique_id`** (first-sight), so `resolve()` →
`_allocate_dir(slug, enum)` ran. `_allocate_dir`
([camera/registry.py:286]) calls `_used_dirs()`, which counts a dir as
used if **any** record references it — **with no liveness filter**. The
bare slug is "used" by the stale record, so it disambiguated to
`arducam-ov9782-usb-camera-15`, orphaning the live camera from its real
`calibration.json`.

The docstring at [camera/registry.py:292] claims disambiguation happens
"only when the bare slug is already owned by **another live record**" —
but the code never checks liveness. **The "live" qualifier is not
implemented.** `adopt_existing_dirs()` (the old cleanup path) is now a
no-op ([camera/registry.py:381]), so nothing prunes the stale owner.

## Contributing factor

The same physical camera got a new `unique_id` purely because its USB
`location` changed on re-plug (its `serial` is `null`, so location is
the only discriminator). Identity should ideally survive a re-plug when
vid+pid (and, when present, serial) match. That's a related but separate
weakness — file/track independently if pursued.

## Fix direction

- `_used_dirs()` / `_allocate_dir` must only treat a slug dir as owned
  when a **live** record holds it. Define "live" (e.g. `last_seen`
  within a TTL, or a matching running `pid`), so a first-sight camera
  re-adopts a slug dir whose only claimant is stale — keeping its
  calibration.
- Provide a way to prune/reconcile stale records (the intent behind the
  now-no-op `adopt_existing_dirs`), or fold reconciliation into
  `resolve()`.
- Consider making identity stable across re-plug when vid+pid(+serial)
  match, so a new USB location doesn't mint a new `unique_id`.

## Downstream symptom #3: incomplete auto-created `<slug>-<enum>/config.json` breaks parallax

Because the camera runs as `...-15`, its config is read from
`<config_dir>/arducam-ov9782-usb-camera-15/config.json`
([calibration/calibration.py:467], keyed on `Path(camera_dir).name`).
Linking a playfield with `set_camera_playfield` **auto-creates** that
`-15/config.json` with only `device_name`, `playfield`, `resolution` —
it does **not** copy the developer-owned `camera_position` (camera
height) from the bare-slug config. So the loaded calibration has
`camera_position = None`, and the parallax gate
([daemon/camera_pipeline.py:1047], `if camera_position and tag_h > 0.0`)
is skipped even when the tag height is correctly registered
(tag 100 @ `z_cm: 12.0`). Observed: tag 100 reported the raw
homography position (~62 cm) instead of the parallax-corrected ~56 cm.

Recovery: add `camera_position` (height + offsets) to the
`-15/config.json` and reload — tag 100 then reads 56.0 cm (matches the
formula `wx*(1 - h/H)` with H=127, h=12). Root fix is the same: don't
mint a `-15` id for a re-plugged camera whose only bare-slug claimant is
stale. Additionally, `set_camera_playfield` (and any config auto-writer)
should carry forward existing developer-owned keys (`camera_position`,
`settings`, `static_marker_ids`) rather than writing a minimal stub.

## Immediate recovery (operational, no code change)

Remove the stale `...1000c456366` record from `registry.json` (or point
the live record's `dir` back to the bare `arducam-ov9782-usb-camera`)
and restart the daemon so the live camera re-adopts its calibration dir
AND its full config (playfield link, `camera_position`, settings). This
fixes all three downstream symptoms (orphaned calibration, missing
playfield link, missing `camera_position`/parallax) at once.
