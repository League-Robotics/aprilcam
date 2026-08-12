---
status: in-progress
sprint: 028
tickets:
- 028-001
- 028-002
- 028-003
- 028-004
- 028-005
- 028-006
---

# Tag generation and real daemon discovery

## Description

Two independent gaps, paired because each is small and both are
"the system cannot do a thing its own docs say it does."

### 1. Tag generation — `aprilcam tags generate` (UC-21)

`src/aprilcam/tags/` contains **`interfaces.py` and nothing else** —
zero implementation files. `TagGenerator` declares `valid_ids(family)`
and `generate(family, ids, *, size_cm, out_dir, dpi)`, and no CLI
command exists.

This is how a playfield gets built in the first place: printable
AprilTag 36h11 and ArUco 4x4 images at a **known physical size**, so
the printed marker matches the surveyed definition (`main-playfield`'s
corner ArUcos) and the `size_cm` registered for a robot's tag (UC-12).
Purely offline — no daemon, no camera, no network — and it must work on
a bare install, which is why OpenCV is a base dependency (UC-21's
recorded decision, reversing an earlier plan to hide it behind the
`[daemon]` extra).

Contracts already specified: an id outside the family's dictionary
range raises `InvalidTagId` naming the valid range and writes nothing
(UC-21.S-3); an unwritable output path fails **before** generating; a
size unachievable at the target DPI **warns** rather than silently
producing an unusable tag. Open: PDF output for multi-tag sheets, as v1
had.

Mine `archive/` — v1 had `taggen`/`arucogen` and the dictionary
handling is earned knowledge, particularly the ArUco API compatibility
shim already ported to `vision/aruco_compat.py`.

### 2. Real mDNS discovery and `aprilcam daemon list` (UC-13)

`Discovery.find_all()` is **stubbed local-only** — sprint 020's
Architecture Decision 2, taken because no in-scope command needed
multi-daemon discovery at the time. The consequence has since become
visible: **`--all` fan-out can never find a second daemon**, so every
fan-out-capable command silently degenerates to "the local one," and
`aprilcam daemon list` does not exist at all.

That matters for the deployment the whole design is shaped around:
`adapters.md` and spec §3 describe a Pi sitting over the field with
remote clients collaborating through it. Finding that Pi is `daemon
list`'s job.

Contracts already specified: finding nothing is an **empty result, not
an error** — a field with nothing running is normal. Discovery
*failing* (multicast blocked) must be reported **distinctly** from
finding nothing (UC-13.S-5); `DiscoveryFailed` already exists in
`errors.py` for exactly this, and collapsing the two sends someone
hunting a dead daemon instead of a network problem. A daemon that
advertises but does not answer is **listed and flagged**, not filtered
out — that is precisely what a developer needs to see.

`archive/src/aprilcam/daemon/mdns.py` is the conversion source.

## Cause

Both were scoped out of earlier sprints with recorded reasons — tag
generation because the stakeholder's playfield already existed, mDNS
because sprint 020 had no command that needed it. Neither reason still
holds.

## Verification

- Generate ArUco 4x4 ids 1–10 at the surveyed size; print one and
  confirm the daemon detects it on the real field.
- An out-of-range id errors naming the range and writes nothing.
- With a second daemon running (a Pi, or a second machine), `aprilcam
  daemon list` finds it, `--all` fan-out labels both, and one
  unreachable daemon is reported alongside results that did come back.
- With multicast blocked, discovery reports *failure*, not emptiness.

## Related

- `src/aprilcam/tags/interfaces.py`, `src/aprilcam/client/discovery.py`
- `docs/design/use-cases/cli.md` UC-13, UC-21, and the fan-out table
- `docs/design/specification.md` §4.10
- Sprint 028 in `docs/design/development-plan.md`'s sequence
