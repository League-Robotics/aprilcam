---
id: 039
title: Reliable uncalibrated cameras with image-space tag yaw and a follow mode
status: done
branch: sprint/039-reliable-uncalibrated-cameras-with-image-space-tag-yaw-and-a-follow-mode
use-cases: []
issues:
- uncalibrated-tag-records-should-report-image-space-yaw-in-degrees.md
- camera-reopen-reuses-a-stale-opencv-index-and-can-open-the-wrong-camera.md
- camera-tags-needs-a-follow-mode-that-streams-detections.md
- camera-probe-reports-a-held-open-camera-as-usable-without-checking-it.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 039: Reliable uncalibrated cameras with image-space tag yaw and a follow mode

## Goals

Make an uncalibrated camera pointed at AprilTags with no playfield a
reliable, continuously-watchable source of tag rotation data, and make
camera identity survive USB drop/reconnect without silently serving
one camera's frames under another camera's name.

After this sprint: `aprilcam camera tags 7 --follow` on such a camera
streams tag id, pixel position, and image-space yaw in degrees,
continuously, with timestamps. A camera that drops off the bus and
comes back never silently serves another camera's frames, and
`camera probe`/`camera list` report the true health of a held-open
camera rather than an unconditional `usable=True`.

## Problem

The stakeholder is running two new cameras (`streaming-camera`,
`global-shutter-camera`) with no playfield and no calibration, purely
to watch and record tag rotations. Four concrete gaps surfaced during
that use, each filed as its own issue:

1. Uncalibrated tag records carry `pixel_corners` but no orientation
   at all — `_build_tag_record` sets `yaw_rad=None` and the CLI prints
   nothing, even though the front edge needed to compute an
   image-space yaw is already known.
2. `camera tags` is a one-shot burst, which both fails to serve the
   "watch rotation over time" use case and interacts badly with the
   temporal confirmation filter (`_confirm_detections`, streak of 3) —
   the first burst after any gap reports `(no tags detected)` even
   with tags plainly in view.
3. On an overloaded USB 2.0 hub, cameras leaving and rejoining the bus
   shift AVFoundation's positional indices. The daemon reopens by a
   cached index rather than re-resolving `hardware_key`, and in the
   observed incident this caused slot 7 (`streaming-camera`) to
   silently serve `global-shutter-camera`'s frames after a bus event —
   defeating the identity guarantees `camera/identity.py` exists to
   provide.
4. `camera probe` short-circuits any active slug to `usable=True`
   without consulting the held-open `Camera`'s own stall/reopen state,
   so an operator is told a stalled camera is fine right up until the
   next `camera tags` call fails.

## Solution

Four issues, addressed together because they share one stakeholder
scenario (uncalibrated cameras, continuous tag-rotation observation)
and two of them are load-bearing for the other two (yaw computation
feeds follow mode's output; identity-safe reopen feeds honest
probe/list health):

1. **Image-space yaw** (`uncalibrated-tag-records-should-report-image-space-yaw-in-degrees.md`) —
   derive an image-space yaw in degrees from `pixel_corners`' front
   edge (`corners[0]->corners[1]`, the same edge `_front_edge_yaw`
   uses in world space) for uncalibrated records, as a new, distinct
   `TagRecord` field (`image_yaw_deg`) so an uncalibrated record never
   looks calibrated. 0 = toward the top of the image; sign convention
   documented in `types.TagRecord`'s docstring. CLI and MCP
   (`get_tags`/`get_tag`/`get_tag_history`) both carry it.
2. **`camera tags --follow`** (`camera-tags-needs-a-follow-mode-that-streams-detections.md`) —
   a CLI mode subscribing to the daemon's existing `stream_tags`
   producer, printing one row per tag (id, timestamp, pixel position,
   image yaw) continuously until interrupted, with an optional
   `--json` for machine-readable recording. This is the mode the
   confirmation filter was designed for, and it depends on (1) for the
   yaw field it prints.
3. **Identity-safe reopen** (`camera-reopen-reuses-a-stale-opencv-index-and-can-open-the-wrong-camera.md`) —
   every open and reopen re-resolves the OpenCV index from the
   record's `hardware_key` via a fresh `resolve_all()` immediately
   before opening, rather than reusing a cached index. A hardware key
   that is not currently enumerated reports `present=False` and keeps
   retrying identity, not the old index. Where the backend can tell
   (AVFoundation uniqueID), verify the opened device's identity after
   open and refuse loudly on mismatch.
4. **Honest probe/list health** (`camera-probe-reports-a-held-open-camera-as-usable-without-checking-it.md`) —
   for an active slug, `usable` reflects the live `Camera`'s own
   health (last-frame age within the stall threshold, reopen budget
   not exhausted) instead of an unconditional `True`; `camera list`
   shows the same signal, and the `skipped` reason distinguishes
   "healthy and in use" from "in use but stalled."

## Success Criteria

- `aprilcam camera tags 7 --follow` on an uncalibrated camera streams
  rows with tag id, timestamp, pixel position, and image-space yaw in
  degrees, continuously, without requiring repeated manual bursts.
- The image-space yaw convention (0 = top of image, documented sign)
  is implemented in the daemon, exposed as a distinct field from
  `yaw_rad`, and surfaced through both the CLI and MCP
  (`get_tags`/`get_tag`/`get_tag_history`).
- A camera whose hardware key drops off the bus and later re-enumerates
  at a different OpenCV index never has its slot silently reassigned to
  a different physical camera; a hardware key that is absent is
  reported as `present=False`, not retried at a stale index.
- `camera probe` and `camera list` report a stalled held-open camera as
  unhealthy, not `usable=True`, and distinguish that from a healthy
  in-use camera in the `skipped` reason.
- Tests cover: image yaw for the four cardinal facings plus one
  oblique; reopen behavior when a device is removed between open and
  reopen (must not open the shifted device); probe/list health
  reporting for a stalled active camera.

## Scope

### In Scope

- `daemon/detection.py`: `_build_tag_record`, front-edge image-space
  yaw computation.
- `types.py`: `TagRecord.image_yaw_deg` field and docstring sign
  convention.
- `cli/camera.py`: `_format_tag_row` (image yaw for uncalibrated rows),
  new `camera tags --follow` (and `--json`) mode.
- `daemon/service.py`: `_camera_for`, `stream_tags` consumption path
  for follow mode, stall watchdog / reopen identity re-resolution.
- `camera/discovery.py`: `probe()` health reporting for active slugs.
- `camera/identity.py`: `resolve_all()` used on every open/reopen, not
  cached.
- `camera/camera.py`: reopen budget / identity verification on open.
- `mcp/tools/perception.py`: `get_tags`/`get_tag`/`get_tag_history`
  carrying `image_yaw_deg`.

### Out of Scope

- Calibrated (world-frame) yaw behavior — `yaw_rad` and its existing
  contract are unchanged.
- Playfield creation, homography, or deskew — this sprint is scoped to
  the no-playfield, uncalibrated-camera use case.
- USB hub hardware fixes or udev/host-level camera stability work —
  this sprint addresses software-side identity correctness, not the
  underlying flaky bus.
- Multi-camera compositing (removed, out of project scope entirely).

## Test Strategy

Unit tests on recorded/synthetic `pixel_corners` sets for image-space
yaw (four cardinal facings + one oblique), unit/integration tests for
identity re-resolution using an enumeration snapshot with a device
removed between open and reopen, and tests for probe/list health
reporting against a simulated stalled active camera. CLI follow-mode
output format gets a smoke test against a fixture stream. Camera and
integration tests that require live hardware remain excluded from the
offline gate per project convention (`-m "not camera and not
integration"`); the two real cameras remain available for manual
verification of `--follow` end to end.

## Architecture

**Substantial** — this sprint touches 4+ existing modules (`types.py`,
`daemon/detection.py`, `daemon/service.py`, `cli/camera.py`,
`camera/discovery.py`, `camera/identity.py`, `camera/camera.py`,
`mcp/tools/perception.py`) and changes the shared `TagRecord` data
model (a new `image_yaw_deg` field). Per this project's opt-in to the
persistent per-subsystem design-doc set
(`Project.design_docs_opt_in: True`), the full architecture write-up
lives in this sprint's `design/` overlay rather than in this section —
see `clasi/sprints/039-reliable-uncalibrated-cameras-with-image-space-tag-yaw-and-a-follow-mode/design/`
for the edited copies of `docs/design/design.md`,
`src/aprilcam/DESIGN.md`, `src/aprilcam/camera/DESIGN.md`,
`src/aprilcam/daemon/DESIGN.md`, `src/aprilcam/cli/DESIGN.md`, and
`src/aprilcam/mcp/DESIGN.md`, each carrying its own Design
Rationale/Migration Concerns for the piece of this sprint it covers.

### Architecture Overview

Four issues, one stakeholder scenario, addressed as one coherent change
because two are load-bearing for the other two:

1. `TagRecord` gains `image_yaw_deg` (`types.py`), computed in
   `daemon/detection.py`'s `_build_tag_record` from the same
   `corners[0]->corners[1]` front edge `_front_edge_yaw` already uses
   in world space, but in raw pixel space and a distinct sign
   convention (0 = top of image, clockwise-positive as seen on
   screen). Populated for every record with `pixel_corners`, calibrated
   or not — it rides the existing `TagFrame`/`TagRecord` wire shape, so
   every existing consumer (raw-stream tag stream, `GetTags`/`GetTag`/
   `GetTagHistory`) carries it with no RPC/proto change.
2. `cli/camera.py` gains `camera tags --follow`/`--json`, the first CLI
   consumer of the daemon's existing `stream_tags`/`GetTagStream` RPC —
   a pure client addition, no daemon-side streaming change needed
   beyond (1) riding along automatically.
3. Camera reopen (`camera/camera.py`'s self-heal, and
   `daemon/service.py`'s stall watchdog respawn) stops reusing the
   index baked into a backend at construction and instead re-resolves
   the index from `hardware_key` via `camera/identity.py`'s
   `resolve_all()`, fresh, immediately before every open — not just the
   first one. Where the backend can verify identity post-open
   (AVFoundation `uniqueID`), a mismatch refuses the open rather than
   silently serving the wrong camera's frames.
4. `camera/discovery.py`'s `probe()` (and `daemon/service.py`'s
   `list_cameras()`) stop short-circuiting an active slug to
   `usable=True` and instead consult the live `Camera`'s own health.

### Design Rationale

See each edited overlay document's own Design Rationale section for the
decision-by-decision detail (index-resolution hook shape, the
`CameraDiscovery` health-accessor signature change, why `--json` is a
narrowly-scoped exception to `cli/`'s "no machine-readable output"
convention, and why no new daemon RPC is needed for follow mode).

### Migration Concerns

No wire/proto change (`image_yaw_deg` rides the existing `TagFrame`
message as a new field; the `GetTagStream` RPC already exists).
`CameraDiscovery`'s constructor signature changes (an `active_slugs`
callback becomes a richer per-slug health accessor) — every direct
constructor call site and test fake updates in the same ticket that
makes the change, no straddling. `OpenCVBackend`'s construction gains
an optional identity-resolution hook, backward compatible for any
caller (tests, `testdev/capture_matrix.py`) that still constructs it
with a bare index. No data migration; no on-disk schema change.

## Use Cases

### SUC-001: Watch an uncalibrated camera's tag rotation, continuously, in degrees
Parent: UC-20 (List the tags on the playfield), extending UC-20.S-3
(Uncalibrated camera)

- **Actor**: Developer/robotics operator (A-03) watching AprilTags with
  no playfield and no calibration.
- **Preconditions**: Daemon running; camera present; no calibration or
  playfield link required.
- **Main Flow**:
  1. Operator runs `aprilcam camera tags <camera> --follow`.
  2. The CLI subscribes to the daemon's tag stream (`GetTagStream`/
     `stream_tags`) and prints one row per tag per frame — tag id,
     timestamp, pixel position, and image-space yaw in degrees — until
     interrupted (Ctrl-C).
  3. With `--json`, each row is instead one JSON object per line.
- **Postconditions**: None — a read; closing the stream unsubscribes
  daemon-side on the next publish tick, per `client/streams.py`'s
  existing contract.
- **Acceptance Criteria**:
  - [ ] `camera tags <camera> --follow` streams continuously with no
        manual repeated invocation, using the existing confirmation
        filter's intended mode (the one-shot path's `(no tags
        detected)`-after-a-gap behavior is unaffected — no change to
        one-shot `camera tags`).
  - [ ] Each row shows tag id, timestamp, pixel position, and
        `image_yaw_deg`, for an uncalibrated record.
  - [ ] `--json` emits one machine-parseable JSON object per line with
        the same fields.
  - [ ] `--follow --all` is refused up front (a single blocking stream
        loop cannot fan out the same way a one-shot RPC does).

### SUC-002: An uncalibrated tag record reports its own image-space orientation
Parent: UC-20 (List the tags on the playfield), extending UC-20.S-3

- **Actor**: Developer (CLI) and AI agent (MCP), either reading tag
  records from an uncalibrated camera.
- **Preconditions**: Camera present, detecting tags, no calibration.
- **Main Flow**:
  1. `daemon/detection.py`'s `_build_tag_record` derives
     `image_yaw_deg` from the detected `pixel_corners`' front edge for
     every record that has corners — uncalibrated or calibrated alike.
  2. The CLI's one-shot `camera tags` prints it next to the pixel
     position in the `UNCALIBRATED` row.
  3. `get_tags`/`get_tag`/`get_tag_history` (MCP) return it as part of
     the same `TagRecord` shape.
- **Postconditions**: None — a read.
- **Acceptance Criteria**:
  - [ ] `image_yaw_deg` is a new, distinct `TagRecord` field —
        `yaw_rad` (world-frame) is never populated or altered by this
        change.
  - [ ] Sign convention (0 = top of image, clockwise-positive as seen
        on screen) is documented in `TagRecord`'s own docstring.
  - [ ] Tests cover the four cardinal facings plus one oblique, against
        recorded/synthetic `pixel_corners`.
  - [ ] MCP `get_tags`/`get_tag`/`get_tag_history` all carry the field
        with no shape-divergence shim needed (verified against
        `mcp/DESIGN.md` §3's fetch-tool convention).

### SUC-003: A camera slot never silently serves another physical camera's frames
Parent: UC-25 (Keep the camera list current...), extending UC-25.S-3
(Names survive a re-plug)

- **Actor**: Daemon (self-heal / stall watchdog), on behalf of any
  client addressing a camera by slug.
- **Preconditions**: A camera is held open; its hardware key may or may
  not currently be enumerated (bus drop, re-plug, index shift from a
  sibling camera leaving/rejoining).
- **Main Flow**:
  1. A read failure streak or a stalled heartbeat triggers a reopen
     attempt (existing self-heal / stall-watchdog mechanism).
  2. Before opening, the daemon re-resolves the current OpenCV index
     for this camera's `hardware_key` via a fresh `resolve_all()` —
     never the index cached from a previous open.
  3. If the hardware key is not currently enumerated, the reopen
     attempt reports `present=False`/`usable=False` and the watchdog
     keeps retrying identity lookup on its normal interval — it never
     opens a stale index.
  4. If the backend can verify identity after opening (AVFoundation
     `uniqueID`), a mismatch against the expected `hardware_key`
     refuses the open and logs loudly, rather than silently caching a
     wrong-camera handle.
- **Postconditions**: The held camera's frames are always this slot's
  own physical camera's frames, or the slot reports itself unusable —
  never a silent substitution.
- **Acceptance Criteria**:
  - [ ] Test: an enumeration snapshot with the target device removed
        and a different device now occupying its old index — reopen
        must not open the shifted device.
  - [ ] A hardware key that drops off the bus reports `present=False`,
        not a retried stale index.
  - [ ] No change to `camera/identity.py`'s fallback-chain order or
        `hardware_key` format (contractual, per `camera/DESIGN.md`
        §3).

### SUC-004: `camera probe`/`camera list` report a stalled held-open camera honestly
Parent: UC-25 (Keep the camera list current...), extending UC-25.S-4
(Camera attached but unopenable)

- **Actor**: Developer/operator running `camera probe` or `camera
  list` against a daemon that is actively holding a camera open.
- **Preconditions**: A camera is held open (active slug) and its
  capture session has stalled (stale heartbeat / exhausted reopen
  budget) or is healthy.
- **Main Flow**:
  1. `probe()`/`list_cameras()` consult the live `Camera`'s own health
     (heartbeat age, reopen budget) for every active slug, instead of
     unconditionally reporting `usable=True` because the slug is held
     open.
  2. A healthy in-use camera reports `usable=True`, skip reason
     distinguishing "healthy and in use."
  3. A stalled in-use camera reports `usable=False`, skip reason
     distinguishing "in use but stalled."
- **Postconditions**: None — a read.
- **Acceptance Criteria**:
  - [ ] Test: a simulated stalled active camera reports `usable=False`
        from both `probe()` and `list_cameras()`.
  - [ ] The `skipped`/in-use reason text distinguishes healthy-in-use
        from in-use-but-stalled.
  - [ ] `probe()`'s non-disturbance invariant (UC-25.S-7 — never opens
        a backend for an actively-held camera) is unchanged; the health
        read is against already-known `Camera` state, not a fresh
        backend open.

## GitHub Issues

(GitHub issues linked to this sprint's tickets. Format: `owner/repo#N`.)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [ ] Sprint planning document is complete (sprint.md, including its
      Architecture and Use Cases sections)
- [ ] Architecture review passed (or skipped, for changes with no
      architectural impact)
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | TagRecord.image_yaw_deg and daemon-side image-space yaw computation | — |
| 002 | CLI and MCP surfaces carry image_yaw_deg | 001 |
| 003 | Identity-safe camera open/reopen (re-resolve index and verify identity every time) | — |
| 004 | Honest probe()/list_cameras() health for active (held-open) cameras | 003 |
| 005 | camera tags --follow and --json streaming mode | 002 |
| 006 | Guide text and knowledge-base updates for follow mode and identity-safe reopen | 003, 005 |

Tickets execute serially in the order listed. 001/003 have no
inter-dependency and could in principle run in either order or in
parallel; they are listed in issue order for readability. 002 and 004
each depend on their predecessor primarily to avoid same-file merge
churn in `daemon/service.py`/`cli/camera.py`, not because of a hard
functional coupling (call out explicitly in each ticket body). 005
has a genuine functional dependency on 002 (reuses its extended
`_format_tag_row`). 006 is documentation-only and depends on 003 and
005 so it documents shipped, not planned, behavior.
