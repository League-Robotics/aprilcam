---
status: pending
---

# `aprilcam camera config` clearing a field (e.g. `playfield: null`) updates live daemon state but not the on-disk config.json

## Description

Pushing a camera config document that *clears* a field only updates the
daemon's in-memory state; the persisted file on disk keeps the old
value.

Discovered 2026-08-22 while unlinking `arducam-ov9281-usb-camera` from
`main-playfield` (administrative cleanup after a temporary contention
workaround). Pushed:

```json
{"name": "arducam-ov9281-usb-camera", "playfield": null, "settings": {}}
```

via `aprilcam camera config <file>`. The CLI reported success
("pushed camera config 'arducam-ov9281-usb-camera' -- playfield=(none)")
and an immediate `aprilcam camera list` showed `playfield=-`, matching.
But the actual on-disk file at
`<config_dir>/cameras/arducam-ov9281-usb-camera/config.json` still read
`"playfield": "main-playfield"` -- completely unchanged.

Consequence: a daemon restart would revert the live state back to the
stale on-disk value, and any client reading the file directly (rather
than through the daemon) sees the wrong, pre-clear value. This is
exactly the config/state trust model this project relies on elsewhere
(`Directories`, `ConfigLoader`) — a divergence here is a correctness
bug, not just a cosmetic one.

**Setting** `playfield` *to* a real value (`"main-playfield"`) persisted
to disk correctly in the same session — only *clearing* it (pushing
`null`) failed to persist. This smells like an update path that treats
`null`/falsy as "no change" rather than "explicitly clear," possibly in
whatever code merges a pushed `CameraConfig` onto the existing
persisted document. Exact location not yet identified — candidates are
`daemon/service.py`'s `SetCameraConfig`-equivalent handler and
`config/directories.py`'s write path.

## Proposed fix

- Identify the config-push persistence path and confirm it writes the
  *complete* pushed document to disk, including explicit null/cleared
  fields — not a merge that silently skips falsy values.
- Add a regression test: push a camera config with a real playfield
  link, then push again with `playfield: null`, and assert the
  **on-disk** `config.json` reflects the clear — not just the live
  daemon's in-memory/`camera list` view. The existing test suite
  apparently never exercises "push a clearing update," since this
  survived undetected.

## Workaround used at the time

Edited the on-disk `config.json` file directly to correct the drift.

## Related

- Discovered during sprint 031's close-gate hardware verification
  (`arducam-ov9281-usb-camera` was linked to `main-playfield` as a
  temporary camera-contention workaround, then unlinked once resolved).
