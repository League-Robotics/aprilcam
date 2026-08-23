---
status: pending
sprint: '031'
---

# 18 of 19 camera-marked failures were v1 daemon contention — CONFIRMED; 1 is genuinely physical

## RESOLVED — hypothesis confirmed 2026-08-20

With the v1 daemon and its view client stopped, a re-run of the same
sweep gave **1 failed, 41 passed, 7 skipped** (529s), against the
**19 failed, 23 passed, 7 skipped** measured while v1 was live.

**18 of 19 failures were v1 contention.** The hypothesis below is
confirmed: v1 held the shared arducam and the shared control socket, and
v2's fixtures were measuring an environment v1 was reconfiguring
underneath them. Corroborated from both directions — the v1 session
confirmed its pre-isolation daemon resolved to
`/var/folders/.../T/aprilcam-501/control.sock`, the same file v2 dials,
and its stale `aprilcamd.pid` (pid 21548) was found sitting in v2's own
socket directory.

**The 1 remaining failure is genuinely physical, and is NOT contention**:

```
test_camera_golden_path_against_real_daemon_and_camera_3
  matched_ids: [6, 7, 9, 10]
  missing_ids: [1, 2, 3, 4, 5, 8]
```

The arducam can currently see only 4 of `main-playfield`'s 10 ArUco
markers. Sprint 030 ticket 002 matched **all 10** in a live run earlier
the same day, so the view changed between roughly 10:21 and 11:59. With
v1 down and every other camera test passing, the remaining explanation
is a real change to the camera's aim or a physical obstruction. **This
needs someone to look at the hardware**; no code change addresses it.

Consequence for sprint 030 ticket 003: still blocked, but on a different
and much cheaper problem than the one originally suspected. Re-aiming the
camera so all 10 markers are visible is the precondition for re-running
`--with-intrinsics` and settling whether the `fx=310.5` rejection is
camera 3's near-nadir geometry (a real degeneracy) or was itself an
artifact of the contaminated environment.

## Original description

A full `-m camera` sweep during sprint 030 ticket 005 gave **19 failed,
23 passed, 7 skipped**. Ticket 005 correctly scoped these out of its own
work and recommended filing them, which is what this issue is.

The failures are not one problem. They break down as:

- **17 failures** attributed to a single environmental precondition,
  described by ticket 004 as "the physical arducam currently can't see
  the full playfield" and by ticket 005 as "now apparently worse."
- **1 failure** from externally-managed, version-mismatched `aprilcam`
  daemons occupying the default control socket, producing
  `Method not found!` — the known v1/v2 collision.
- **1** addressed by ticket 005 itself (the daemon-reachability guard).

## The hypothesis this issue exists to test first

**A v1 daemon and a v1 view client are running right now**, from the
sibling checkout, and they are using the cameras:

```
/Volumes/Proj/proj/RobotProjects/AprilTags/.venv/bin/python3 -m aprilcam.daemon
/Volumes/Proj/proj/RobotProjects/AprilTags/.venv/bin/aprilcam view b
```

Before accepting "the camera was physically bumped" as the explanation,
rule this out — it accounts for more of the evidence:

1. **The visibility failures.** If v1's daemon holds the same UVC device
   and applies its own settings (resolution, zoom, crop, exposure), a v2
   test opening that device sees a differently-configured frame than the
   one its fixtures assume. "Can't see the full playfield" is what a
   changed capture format looks like from the outside. No one has to
   have touched the camera mount.

2. **The intermittent UVC write rejections.** Ticket 005 falsified all
   four candidate causes for
   `uvc-util -s exposure-time-abs=300 exited 13` and concluded it was a
   non-deterministic hardware/driver rejection, reproduced twice, both
   times succeeding on immediate retry. **Two processes writing the same
   UVC controls produces exactly that signature.** A live v1 daemon
   pushing its own settings to a shared device is a more specific
   explanation than driver flakiness, and it was not considered because
   the v1 daemon's presence was not part of that investigation's frame.

3. **The `Method not found!` failure** is already understood as the v1/v2
   collision and needs no new theory — but it is the same root cause as
   (1) and (2) if this hypothesis holds, which would collapse three
   separately-filed symptoms into one.

## How to test it

Cheaply, in order:

1. Confirm what the v1 daemon actually has open, and whether `camera b`
   in v1 is the same physical device as `arducam-ov9782-usb-camera` in
   v2.
2. With the stakeholder's agreement — **do not stop it unilaterally, the
   `aprilcam view b` window suggests it is in active use** — stop the v1
   daemon and its view client, then re-run the `-m camera` sweep. If the
   17 failures collapse, the hypothesis holds and this is not a field
   problem at all.
3. Only if they persist, inspect the physical mount and the camera's
   field of view.

## Why this matters beyond the test suite

Sprint 030 ticket 002 ran `--with-intrinsics` live against camera 3 and
had the fit rejected for an implausible focal length
(`fx=310.5px`, 0.24x the 1280px image width). That was attributed to
camera 3's near-nadir mount, which is a real and independent geometric
degeneracy — but a camera that is also being reconfigured underneath the
measurement is a second, uncontrolled variable in the same experiment.
**Sprint 030 ticket 003's re-verification should not be attempted until
this is resolved**, or it will measure an environment nobody can
reproduce.

## Related

- `configurable-daemon-instance-identity-for-side-by-side-v1-v2.md`
  (currently in `later/`) — the structural fix. This issue is evidence
  that the collision is not hypothetical: it is actively costing test
  signal today.
- Sprint 030 tickets 002, 004, 005 completion notes — the three
  independent observations this issue ties together.
