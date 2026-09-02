---
id: '037'
title: Flat-field gain map capture as a daemon workflow
status: executing
branch: sprint/037-flat-field-gain-map-capture-as-a-daemon-workflow
use-cases: []
issues:
- flat-field-gain-map-capture-as-a-daemon-workflow.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 037: Flat-field gain map capture as a daemon workflow

## Goals

Turn flat-field gain-map **capture** into a first-class daemon workflow,
matching the **apply** side that already shipped out-of-process (commit
`7f4c0ac`: `Camera.read()` multiplies frames by
`<state_dir>/cameras/<slug>/flatfield.npy`, reloaded on `open()`/`reset()`).
Today, building a map is a manual recipe an operator runs by hand
(`docs/knowledge/flat-field-gain-map.md`,
`docs/knowledge/assets/build_flatfield_2026-09-02.py`): push a low-exposure
override, grab frames via `get_frame`, run a scratch OpenCV script with a
hard-coded field polygon, copy the result into the state dir, and reset.
This sprint replaces that recipe with a repeatable `CaptureFlatField`
workflow that anyone (or an agent) can run end-to-end.

## Problem

The room's lighting is uneven and cannot easily be fixed (measured
2026-09-02: camera 3 sees a 2.65x light-field spread plus a ~30% green
deficit; camera 5 sees 1.32x — see `docs/knowledge/flat-field-gain-map.md`).
The correction (a per-pixel, per-channel multiplicative gain map) is
already applied automatically once a map exists, but producing that map
requires an operator to hand-run a script with hard-coded paths and a
hard-coded field polygon. There is no daemon RPC, no CLI command, no MCP
tool, and no quality check — a bad capture (clipped frames, a sheet that
moved, an implausible spread) silently produces a bad map with nothing to
catch it.

## Solution

Add a daemon-side `CaptureFlatField` RPC (proto, `daemon/rpc.py`, client
wrapper, MCP tool, and a CLI `aprilcam camera flatfield <cam> [--exposure N]
[--frames K] [--clear] [--dry-run] [--force]`) that runs the full sequence
inside the daemon process: push a temporary exposure override, capture K
frames, normalise each to the batch mean (lights flicker ~25% frame-to-frame
at short exposures), average, median-blur to suppress sheet wrinkles and
specular highlights, fill outside the field with the field mean, wide
Gaussian to keep only the low-frequency light field, per-channel
`gain = mean / L`, clip to a sane range, write `flatfield.npy` atomically,
restore the working config, and reload the map live — the same pattern
`calibrate` already follows. The field-interior mask comes from the linked
playfield (calibration homography, or detected ArUco corners when
uncalibrated; refuse if neither is available) rather than a hard-coded
polygon. A quality gate reports light-field spread (p98/p2) per channel
before and after, and refuses without `--force` on in-field clipping,
implausible spread, or frame-to-frame disagreement beyond flicker. Flat-field
state (present, captured_at, frame shape, gain range, stale flag) is
surfaced wherever calibration state already is.

All pixel math stays daemon-side — the CLI and MCP server remain cv2-free
thin clients per the sprint 015 boundary
(`tests/test_no_cv2_in_client_modules.py`).

## Success Criteria

- An operator (or agent) can capture, inspect, and clear a flat-field map
  for a camera through the CLI/MCP surface alone — no manual script, no
  hard-coded paths or polygon.
- The capture workflow refuses a bad capture (clipping, implausible
  spread, sheet movement) unless `--force` is passed, and reports the
  measured spread before/after either way.
- Flat-field presence/staleness is visible next to calibration state in
  the existing camera-config reporting surface.
- `tests/test_no_cv2_in_client_modules.py` continues to pass — no OpenCV
  import leaks into the CLI or MCP server.
- Synthetic-frame unit tests cover the map-building math; fake-backend RPC
  round-trip tests cover the daemon workflow; one `hardware`-marked test
  exercises a real capture and skips cleanly without the rig.

## Scope

### In Scope

- Daemon-side `CaptureFlatField` RPC: proto definition, `daemon/rpc.py`
  handler, client wrapper, MCP tool, CLI subcommand
  (`aprilcam camera flatfield <cam> [--exposure N] [--frames K] [--clear]
  [--dry-run] [--force]`).
- Capture sequence: temporary exposure override, K-frame capture,
  per-frame mean normalisation, averaging, median blur, field-mean fill
  outside the mask, wide Gaussian, per-channel gain computation, clipping,
  atomic `flatfield.npy` write, config restore, live reload.
- Field-interior mask derived from the linked playfield's calibration
  homography, or from detected ArUco corners when uncalibrated; refusal
  when neither is available.
- Quality gate: p98/p2 spread reporting before/after per channel; refusal
  (absent `--force`) on in-field clipping, implausible spread (e.g. >10x),
  or frame-to-frame disagreement beyond expected flicker.
- Flat-field state surfaced alongside calibration state in
  `list_cameras` / `get_camera_config`-style reporting, including a
  `stale` flag set on the same events that already stale a calibration
  (camera reopen, a resolution-changing config push).
- `--clear` support: delete the stored map and clear the live-applied map.
- Tests: synthetic-frame unit tests for the map-building math, fake-backend
  RPC round-trip tests, one `hardware`-marked real-capture test skipped
  cleanly without the rig.

### Out of Scope

- Automatic detection that room lighting has changed since a map was
  captured (the existing "recapture is a two-minute manual job" gotcha
  stands).
- Adding more lights to fix camera 3's northeast corner at the hardware
  level — the gain map is the software workaround, not the physical fix.
- The apply side (already shipped out-of-process in commit `7f4c0ac`) —
  this sprint is capture-only.

## Test Strategy

Unit tests exercise the map-building math against synthetic frames with a
known gradient (must flatten to within tolerance), synthetic wrinkle/noise
patterns (must not leak into the map), and synthetic clipped frames (must
be refused). RPC round-trip tests run the `CaptureFlatField` sequence
against the fake backend used elsewhere in the daemon test suite,
verifying the client/MCP/CLI surfaces and the quality-gate refusal paths
without needing real hardware. A single `hardware`-marked test exercises
an actual capture against the rig and is skipped cleanly (not failed) when
no camera is attached, per the project's existing `camera`/`integration`
marker conventions (see `docs/knowledge/project_sprint_close_test_gate.md`
convention: offline gate excludes `camera and integration`-marked tests).

## Architecture

**Substantial** — new daemon-side capture engine (`calibration/flatfield.py`),
a new cross-module dependency (`daemon/` → the new module, mirroring the
existing `daemon/` → `calibration/calibration.py` dependency), three new
RPCs (proto + `daemon/rpc.py` + client + MCP + CLI), and a `CameraState`
data-model addition (`flatfield_present`/`flatfield_stale`). This project
has opted into the persistent per-subsystem `docs/design/` doc set
(`Project.design_docs_opt_in: True`), so the full architecture write-up
lives in this sprint's `design/` overlay (`clasi/sprints/037-.../design/`)
rather than in this section — see that overlay's edited copies of
`design.md`, `daemon/DESIGN.md`, `calibration/DESIGN.md`,
`camera/DESIGN.md`, `client/DESIGN.md`, `cli/DESIGN.md`, and `mcp/DESIGN.md`,
and their `.diff.md` siblings, for the full Planned Changes / Rationale /
Impact / Migration Concerns content and the component diagram.

Summary for a reader of this file alone: `calibration/flatfield.py` adds a
`FlatFieldCapturer` (mirrors `Calibrator`'s injected-frame-source, pure-math
shape) that builds and quality-gates a gain map from a captured frame burst
plus a field-interior mask; `daemon/service.py` gains a thin orchestrator
(`capture_flat_field`/`get_flatfield`/`clear_flat_field`) that resolves the
mask (calibration homography, or ArUco corner detection when uncalibrated),
pushes/restores a temporary exposure override, and persists the result
next to `calibration.json` (`flatfield.npy` plus a new `flatfield.json`
metadata sidecar, mirroring how `calibration.json` already carries
`stale`). No existing RPC, message, or CLI command changes shape — this is
additive throughout.

### Migration Concerns

None — purely additive. No existing `flatfield.npy` file (the apply side,
commit `7f4c0ac`) changes shape or location; a camera with a pre-existing
map from the manual recipe is picked up unchanged, and this sprint adds
only the new `flatfield.json` metadata sidecar (absent for a map produced
by the old manual script until the next capture through this workflow —
`get_flatfield` reports `present=True` with `captured_at=None` in that
one case, never a fabricated timestamp). See the `design/` overlay's
edited `calibration/DESIGN.md` and `daemon/DESIGN.md` for the full
Migration Concerns discussion.

## Use Cases

Substantial sprint — full use cases. No existing UC in
`docs/design/use-cases/` covers radiometric (lighting/colour-cast)
correction; the closest neighbours are UC-04/UC-15 (calibration, the
pixel↔world artifact this workflow's RPC/CLI/quality-gate shape is
modelled on) and UC-03 (camera config, for the temporary exposure
override this workflow pushes and restores). SUC-001/002/003 below are
therefore new capability, not an extension of an existing UC.

### SUC-001: Capture a flat-field gain map for a camera
Parent: none (new capability; modelled on UC-04/UC-15's calibration shape)

- **Actor**: Operator or agent, via CLI or MCP
- **Preconditions**: A white sheet (or equivalent uniform reference) covers
  the camera's field of view. The camera is enumerated and reachable. The
  camera is either calibrated against a linked playfield, or its ArUco
  corner markers are visible and detectable (uncalibrated fallback).
- **Main Flow**:
  1. Caller requests `CaptureFlatField` for a camera, optionally naming an
     exposure override and a frame count (defaults apply otherwise).
  2. The daemon derives the field-interior mask from the linked
     playfield's stored calibration homography, or — absent a
     calibration — from detected ArUco corner markers.
  3. The daemon pushes a temporary low-exposure override, captures K
     frames, and restores the camera's prior working configuration
     (always, even on failure below).
  4. The daemon normalises each frame to the batch mean (correcting for
     ~25% frame-to-frame light flicker), averages, median-blurs to
     suppress sheet wrinkles/specular highlights, fills outside the mask
     with the field mean, and Gaussian-blurs to isolate the low-frequency
     light field.
  5. The daemon computes a per-channel gain map (`gain = target / L`),
     clips it to a sane range, and evaluates the quality gate (SUC-002).
  6. On a passing gate (or `force=True`), the daemon writes
     `flatfield.npy` (and its metadata sidecar) atomically and reloads the
     live map on the held-open camera with no restart.
  7. The daemon reports the measured light-field spread (p98/p2) per
     channel, before and after correction.
- **Postconditions**: A valid flat-field gain map is installed and
  applied to every subsequent frame `Camera.read()` returns for that
  camera; the daemon's flat-field state for the camera reports
  `present=True`, `captured_at`, shape, and gain range.
- **Acceptance Criteria**:
  - [ ] A calibrated camera derives its field mask from the stored
        calibration homography; an uncalibrated camera with visible ArUco
        corners derives it from detection; a camera with neither is
        refused with a clear reason and no capture attempt.
  - [ ] The camera's working configuration (in particular exposure) is
        restored after capture, whether the capture succeeds, fails the
        quality gate, or raises.
  - [ ] The measured p98/p2 spread per channel is reported both before
        and after correction, on every call (success, refusal, or
        `--dry-run`).
  - [ ] `--dry-run` evaluates and reports without writing `flatfield.npy`
        or changing the live-applied map.
  - [ ] A successful capture is visible immediately (no restart) via a
        subsequent `get_frame`/detection call on the same camera.

### SUC-002: Quality gate refuses a bad capture
Parent: none (new capability)

- **Actor**: The daemon, during `CaptureFlatField`
- **Preconditions**: A capture burst has been taken (SUC-001 steps 1-4).
- **Main Flow**:
  1. The daemon checks whether any captured frame clips (saturates)
     inside the field mask.
  2. The daemon checks whether the measured light-field spread is
     implausible (e.g. more than 10x — consistent with a reference sheet
     not actually covering the field).
  3. The daemon checks whether the frames disagree with each other beyond
     the expected flicker tolerance (consistent with the sheet moving
     mid-capture).
  4. Any one of these failing refuses the capture — `flatfield.npy` is
     left untouched (a good prior map, if any, survives) — unless the
     caller passed `force=True`.
- **Postconditions**: A refused capture makes no filesystem write and
  does not touch the live-applied map. A forced capture proceeds despite
  a failing check, and the returned result says which check(s) it
  overrode.
- **Acceptance Criteria**:
  - [ ] In-field clipping in any captured frame refuses without `force`.
  - [ ] Greater than 10x measured spread refuses without `force`.
  - [ ] Frame-to-frame disagreement beyond the flicker tolerance refuses
        without `force`.
  - [ ] `force=True` proceeds through any of the above and the result
        reports which check(s) were overridden.
  - [ ] A refused capture never writes or modifies `flatfield.npy`.

### SUC-003: Inspect and clear flat-field state
Parent: none (new capability; mirrors UC-05's calibration-state
reporting shape)

- **Actor**: Operator or agent, via CLI or MCP
- **Preconditions**: None — every camera the registry knows about is
  reportable, present or not, mirroring UC-25's "an absent camera is
  listed and flagged, not omitted."
- **Main Flow**:
  1. Caller lists cameras or fetches one camera's config; the flat-field
     state (`present`, `captured_at`, frame shape, gain range, `stale`)
     is reported alongside calibration state.
  2. A camera reopen (self-heal or explicit) or a resolution-changing
     config push marks that camera's flat-field state `stale`, exactly
     like the equivalent events already mark a calibration stale.
  3. Caller issues `--clear` (CLI) or the clear RPC (MCP/client): the
     daemon deletes the stored map file and its metadata sidecar and
     drops the live-applied map from the held-open camera, with no
     restart.
- **Postconditions**: After clear, the camera's flat-field state reports
  `present=False`; subsequent frames pass through uncorrected, exactly as
  if no map had ever been captured.
- **Acceptance Criteria**:
  - [ ] `list_cameras`/`get_camera_config`-style reporting surfaces flat-field
        presence, capture time, frame shape, gain range, and staleness.
  - [ ] A camera reopen (self-heal or explicit) marks that camera's
        flat-field state stale, the same event that already marks a
        calibration stale.
  - [ ] A resolution-changing `set_camera_config` push marks the
        camera's flat-field state stale.
  - [ ] `--clear` deletes the on-disk map and its metadata, and the next
        `get_frame`/detection call on that camera sees uncorrected frames
        with no restart.

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
| 001 | Flat-field gain-map math module (`calibration/flatfield.py`) | — |
| 002 | Daemon interface, `DaemonService` orchestration, and flat-field persistence | 001 |
| 003 | Proto messages, `CaptureFlatField`/`GetFlatField`/`ClearFlatField` RPCs | 002 |
| 004 | Client wrapper and `camera flatfield` CLI command | 003 |
| 005 | MCP flat-field tools and camera-state reporting surface | 004 |
| 006 | Hardware-marked capture test and knowledge-doc update | 005 |

Tickets execute serially in the order listed.
