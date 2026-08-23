---
status: pending
---

# `aprilcam camera config` pushes update live daemon state but sometimes don't persist to the on-disk config.json

## Description

Pushing a camera config document sometimes only updates the daemon's
in-memory state; the persisted file on disk keeps the old value. Three
independent occurrences found so far, both live and both self-defeating
(the CLI reports success and an immediate `camera list`/re-query, or
even a live capture, confirms the new behavior took effect, but the
file never changes). The occurrences span a linked camera and an
unlinked one, and both a `settings` update and a `playfield` clear —
**the "unlinked camera" theory from occurrence 2 does not hold up**;
this looks like config-push-to-disk persistence being broadly
unreliable, not conditional on one specific field or link state.

**Occurrence 3 (2026-08-23) — a `settings` push for a *linked, already-
configured* camera silently failed to persist.** Deploying v1's known-
good exposure settings to `arducam-ov9782-usb-camera` (already linked
to `main-playfield`, already had a `settings.exposure` key): pushed
`{"auto_exposure": 1, "exposure": 6, "gain": 2, "auto_white_balance":
0, "white_balance_temperature": 4600}`. CLI reported success; the very
next `camera image` capture was correctly exposed, proving the daemon
applied it live. The on-disk file still read `{"auto_exposure": 1,
"exposure": 100}` — not just missing the three new keys, but not even
updating the *pre-existing* `exposure` key from its stale value. This
rules out "only new keys fail to write" as the mechanism too. See
`docs/knowledge/2026-08-23-v1-known-good-arducam-ov9782-exposure-settings.md`
for the full context this was found in.

**Occurrence 4 (2026-08-23) — inverted: `settings` persisted correctly,
`playfield` silently reverted.** Immediately after occurrence 3, pushed
`arducam-ov9281-usb-camera` with both `settings.exposure: 300` and
`playfield: "main-playfield"` in the same document. Re-reading the file
afterward: `settings` was correct on disk, but `playfield` read back as
`null` -- the opposite of occurrence 2, where `playfield` was the field
being pushed to `null` and *that* didn't persist. **This rules out "one
specific field never persists"** -- across the four occurrences, both
`settings` and `playfield` have each independently failed to persist
in different pushes, sometimes clearing incorrectly, sometimes failing
to set. This now reads as non-deterministic/racy config-write behavior,
not a single deterministic code path with one bad branch.

**Occurrence 2 (2026-08-23) — a `settings` push for an *unlinked*
camera doesn't persist at all.**
Tuning `arducam-ov9281-usb-camera` (a camera with `playfield: null` at
the time) live: pushed `{"auto_exposure": 1, "exposure": 300}`. The
live daemon genuinely took it — the very next capture was correctly
exposed and tag detection worked — but the on-disk `config.json` still
read `"settings": {}`, completely unchanged. A daemon restart would
have silently reverted the camera to unusable (pitch black) with no
warning. Worked around by editing the file directly. Suggests the
persistence gap may specifically be "does this camera need to be
linked to a playfield for a settings push to actually write through,"
not only "clearing a field" as originally filed below — needs
investigation to confirm the real boundary.

**Occurrence 1 (original filing) — clearing a field (`playfield:
null`) doesn't persist.**

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
