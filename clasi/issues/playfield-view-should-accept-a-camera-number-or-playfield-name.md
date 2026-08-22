---
status: pending
---

# `playfield view`/`playfield image` have no way to name the playfield -- only an optional, auto-resolved `--camera`

## Description

`aprilcam playfield view` and `aprilcam playfield image` take no
positional argument at all. The only knob is `--camera`, and its help
text reads:

> camera name (slug or persistent number) -- resolved automatically
> when omitted and exactly one camera is linked to a playfield

That "resolved automatically" clause only fires in the single-camera
case. Once a second playfield/camera exists,
`cli._common.resolve_playfield_camera` (`src/aprilcam/cli/_common.py:220`)
refuses to guess and errors out:

```
error: multiple cameras are linked to a playfield (brio-501, usb2-video)
-- pass --camera to disambiguate
```

So to view a specific playfield, the operator has to already know
which *camera* is linked to it and pass `--camera <camera-name>` --
there is no `--playfield` or positional that lets them just say which
playfield they want. That's backwards for a command whose whole
purpose is "show me this playfield."

## Proposed fix

Replace `--camera` on `view`/`image` with a single required
positional that does double duty:

- If it parses as a number, treat it as the camera's persistent
  number and resolve straight to that camera (this format already
  exists as an accepted camera identifier elsewhere -- `camera view`,
  `camera calibrate`, and `locate` all document their `camera`
  positional as "camera name (slug or persistent number)"; see
  `src/aprilcam/cli/camera.py:493`, `calibrate.py:133`,
  `locate.py:404`).
- If it's a non-numeric string, treat it as a **playfield name** and
  pick the first usable camera linked to it, rather than requiring the
  operator to know the camera at all.

Cameras are fixed/known ahead of time, so this covers both "I know
which camera I want" and "I know which playfield I want" without a
separate flag.

## Open questions for design

- This isn't a plain bug fix -- `playfield.py`'s module docstring
  documents "playfield commands resolve their camera" as a deliberate
  `cli.md` convention from ticket 022-006, and states the *optional*
  `--camera`-with-auto-resolve shape explicitly (not "a gap to close
  silently"). This issue is proposing to change that convention, not
  just patch a bug -- needs a real design pass, not a quick edit.
- What does "first usable camera" mean precisely when a playfield has
  several linked cameras -- first by name sort? First `present`? First
  calibrated? `resolve_playfield_camera` currently treats *any*
  ambiguity as an error rather than picking one; this proposal
  reverses that for the playfield-name case specifically.
- Does `--camera` disappear entirely, or stay as an override alongside
  the new positional? `playfield image`'s docstring also depends on
  `resolve_playfield_camera` (`cli/playfield.py:213`) -- both
  commands need to move together.
- `--all` on `view` is refused up front ("viewing is single-target");
  confirm the new positional doesn't reopen that question when a
  playfield name maps to multiple cameras.
