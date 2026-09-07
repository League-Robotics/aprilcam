---
status: in-progress
sprint: 039
tickets:
- 039-004
---

# `camera probe` reports a held-open camera as usable without checking that it delivers frames

## Description

2026-09-06: `aprilcam camera probe` printed `global-shutter-camera
present usable` and listed it under `skipped: in use by a live
subscriber`, while the very next `camera tags 8` failed with
`no frame available from camera 'global-shutter-camera'`, and the
daemon log showed the camera's stall watchdog had already given up
("respawn FAILED, camera left unusable, retried every 60s").

`camera/discovery.py`'s `probe()` short-circuits any slug in
`active_slugs()` to `usable=True` without consulting the held-open
`Camera`'s own health (its stall/reopen state), so an operator asking
"is this camera usable?" gets the wrong answer for exactly the camera
that is broken.

### Needed

- For an active slug, `usable` must reflect the live `Camera`'s health
  (last-frame age within the stall threshold, reopen budget not
  exhausted), and `camera list` should show the same.
- The `skipped` reason should distinguish "healthy and in use" from
  "in use but stalled".
