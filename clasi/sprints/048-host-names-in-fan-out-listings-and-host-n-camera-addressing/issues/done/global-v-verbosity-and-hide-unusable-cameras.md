---
status: done
sprint: 048
tickets:
- 048-002
---

# Global `-v` verbosity count; `camera list` hides unusable cameras unless `-v`

## Description

Stakeholder request, 2026-11-01. Add a root-level `-v`/`--verbose` option
that may be repeated (`action="count"`), like `-d`. It is set once on the
root parser (`aprilcam -v camera list`) and also accepted after the
subcommand (`aprilcam camera list -v`), without the subcommand copy
shadowing the root value.

Levels:

| Count | Meaning | This sprint |
|---|---|---|
| 0 | normal output | yes |
| 1 (`-v`) | more detail in each command's normal output | yes (`camera list`) |
| 2 (`-vv`) | also show INFO log messages on stderr | wire the level only, or defer |
| 3 (`-vvv`) | also show DEBUG log messages | wire the level only, or defer |

### Needed now

- **`camera list` (and `camera list --all`) hides unusable cameras by
  default.** That is any row whose `usable` is false: absent, or
  present but broken. With `-v`, every camera is shown, as today. When
  rows are hidden, a trailing note says so, e.g. `(3 unusable cameras
  hidden; -v to show)`, so an all-hidden daemon does not look empty.
- `camera calibrate` already has its own `-v/--verbose`. Merge it into
  the global count (count >= 1 means verbose) rather than keep two
  meanings for `-v`.
- `-v` is the verbosity flag the HOST-N issue
  (`host-qualified-camera-names-donald-4.md`) mentions for showing
  daemon addresses in fan-out labels. With `-v`, a label adds the
  address, e.g. `donald (192.168.4.60:5280):`.

### Later (not this sprint unless trivial)

- `-vv` / `-vvv` set the root logger to INFO / DEBUG for CLI-side
  logging (discovery, client).
