---
id: '042'
title: Register USB cameras alongside libcamera loopbacks on Linux
status: executing
branch: sprint/042-register-usb-cameras-alongside-libcamera-loopbacks-on-linux
use-cases:
- UC-25
issues:
- usb-cameras-ignored-on-a-linux-host-that-also-has-libcamera-loopbacks.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 042: Register USB cameras alongside libcamera loopbacks on Linux

## Goals

Fix `camera/identity.py` so that a Linux host with **both** libcamera
loopback cameras (Pi CSI, bridged via `v4l2loopback`) and USB/UVC
cameras gets every camera registered, not just the libcamera ones. USB
cameras are the expected setup going forward, so a host that also
happens to have a working CSI bridge must not silently drop them.

## Problem

`identity.py`'s `resolve_all()` returns early with only
libcamera-resolved identities whenever `_probe_libcamera()` finds any
loopback device, so cv2-enumerated USB/UVC cameras on the same host are
never resolved or registered — confirmed live on vidar.local: enumeration
correctly finds both the USB "Streaming Camera" (`/dev/video0`) and the
`aprilcam-imx296-88000` loopback (`/dev/video70`), but `camera probe`
only ever lists the two libcamera-derived entries.

A second, related hazard was found while tracing the fix: `identity.py`
has two disjoint index domains in play — libcamera's 0-based enumeration
`position` (0, 1, 2, …) and the real `/dev/videoN`-derived index cv2
enumeration uses (0, 70, …, confirmed correct by sprint 041). Naively
merging the two sources by returning both dicts side by side risks a
key collision whenever a real camera's cv2 index numerically coincides
with a loopback's position (e.g. both landing on `0`). Worse,
`resolve_identity(index)` — called directly by `OpenCVBackend.open()`
and `WorkerBackend` as a post-open anti-substitution check — already has
this exact collision latent today on any mixed host, because it probes
`_probe_libcamera()` fresh and looks up `libcamera_entries.get(index)`
using the caller's real cv2 index against the position-keyed dict.

## Solution

Re-key `_probe_libcamera()`'s returned dict by each loopback's real
`/dev/videoN` device index (`LibcameraDevice.device_index`) instead of
its enumeration `position`. On Linux this is the same physical-device
index domain `_enumerate_cv2()` already uses (post sprint-041
backend-offset stripping), so the two sources become directly
comparable: the same loopback device shows up under the *same* integer
key from both sources, and two genuinely different cameras can never
collide (two live V4L2 devices can't share one `/dev/videoN`).

With that one change, `resolve_all()` is fixed by replacing its
either/or early return with a union over both sources' keys, resolving
each through the existing `resolve_identity()` chain — which already
checks the libcamera dict first — so a loopback resolves exactly once,
under its libcamera identity, and every other index falls through to
the normal cv2/UVC fallback chain unchanged. The same re-keying also
closes the `resolve_identity(index)` direct-call collision described
above, with no change needed at either call site (`opencv_backend.py`,
`worker_backend.py`): once the domains are unified, a real camera's
index can only match a libcamera entry when it genuinely *is* that
loopback device.

Loopback-only hosts (no cv2 entries — none today, since libcamera and
cv2 both currently see the loopback) and cv2-only hosts (macOS, or a
Linux host with no `v4l2loopback` bridge — `_probe_libcamera()` returns
`{}`) are unaffected: the union degenerates to exactly one source, same
as today's early return.

## Success Criteria

- On a mixed Linux host, `resolve_all()` returns an identity for every
  physical camera: each libcamera loopback under its `libcamera:`
  identity, each USB/UVC camera under its normal fallback-chain
  identity — each exactly once.
- `camera probe -d vidar.local` lists `imx296`, `imx296-2`, and the USB
  camera, each exactly once, with `imx296`/`imx296-2` keeping their
  existing registry numbers (#1, #2).
- `camera image -d vidar.local <usb-slug>` returns a real frame; `camera
  image` for `imx296` is unaffected.
- Loopback-only hosts, UVC-only hosts, and macOS behave exactly as
  before (no regression).
- Offline test suite covers: mixed host, loopback-only host, UVC-only
  host, a loopback present in both enumeration sources (no duplicate
  registration), and stable registry numbering across the fix.

## Scope

### In Scope

- The merge/dedup fix in `src/aprilcam/camera/identity.py`
  (`_probe_libcamera`, `resolve_all`; `resolve_identity` needs no
  behavior change, only benefits from the re-keyed input).
- A docstring-accuracy fix in `src/aprilcam/camera/backends/__init__.py`
  (`resolve_capture_backend`'s docstring currently says the libcamera
  branch's `index` argument is "position, not device index" — after
  this sprint it *is* the device index, though that branch already
  ignores the passed `index` and re-derives it via hardware_key match,
  so this is a comment-only fix, not a behavior change).
- Offline unit tests in `tests/aprilcam/camera/test_identity.py` (and
  `test_discovery.py` if the merged output needs coverage at that
  layer) for the five scenarios in Success Criteria.
- Live verification on vidar.local: build and deploy a wheel, confirm
  `camera probe`/`camera image` behavior described above, leave the Pi
  in AprilCam-owns-the-cameras final state.
- `src/aprilcam/camera/DESIGN.md` update describing the resolution
  contract (index-domain unification, dedup rule).
- A short note in `docs/knowledge/raspberry-pi-camera-setup.md` that
  USB and CSI cameras coexist on the same host.

### Out of Scope

- Any change to `camera/registry.py`'s reconciliation logic — the
  existing hardware-key-keyed registry already handles "a new hardware
  key shows up" correctly; this sprint only ensures the USB camera's
  hardware key is ever *produced*. Registry numbering stability is a
  verification point, not a code change.
- Any change to `camera/discovery.py` — it already iterates whatever
  `identity_source()` returns; no logic there assumes a host is
  all-libcamera or all-cv2 (confirmed by reading `probe()` and its
  registry-reconciliation loop). Docstring cross-references there that
  describe the old "position" semantics of `resolve_capture_backend`
  are covered by the `backends/__init__.py` docstring fix above, not a
  `discovery.py` change.
- `LibcameraDevice.position` itself is not removed — it stays as the
  enumeration-order field it already documents; only which field
  `identity.py` uses as its internal dict key changes.
- Multi-camera compositing, image processing tools, and anything else
  already out of scope per the project overview.

## Test Strategy

Offline (`-m "not camera and not integration"`): unit tests against
`camera/identity.py` with injected `enum_entries`/`libcamera_entries`
fixtures (no real hardware, no subprocess) covering: (1) mixed host —
one loopback plus one UVC camera, both resolved, each once; (2)
loopback-only host — unchanged from today; (3) UVC-only host —
unchanged from today; (4) a loopback device present in *both*
enumeration sources under the same `/dev/videoN` — resolves once, under
its libcamera identity, never a second `loc:`/`vidpid:` record; (5)
registry numbering stability — an existing registry with libcamera
cameras already numbered keeps those numbers when a USB camera is added
to the mix. `uv run mypy` over `src/aprilcam` as part of the same
offline gate (sprint 041's close failed on an `Optional`-narrowing
mypy error — tickets run mypy directly rather than relying on
`close_sprint` to catch it first).

Live (vidar.local, one ticket): build a wheel, `pipx install --force`
as the `aprilcam` user per `deploy/README.md`, confirm the new code is
on disk before restarting `aprilcamd`, then from the Mac exercise
`camera probe`/`camera image` per Success Criteria. No fabricated
frame inspection — a real frame must be fetched and looked at.

## Architecture

**Compact** — this sprint changes one module's internal behavior
(`camera/identity.py`'s enumeration-merge logic). No new module, no new
or changed cross-module dependency, no dependency-direction change, and
no data-model change: `CameraIdentity` (the frozen dataclass in
`camera/interfaces.py`) is unchanged, and every existing caller of
`resolve_identity`/`resolve_all` keeps the same call signature. A
docstring-only touch-up in `camera/backends/__init__.py` accompanies it
for accuracy but changes no behavior there.

### What Changed

**`camera/identity.py`** — `_probe_libcamera()`'s returned dict changes
its key from each `LibcameraDevice.position` (0-based enumeration order)
to its `device_index` (the real `/dev/videoN` number). `resolve_all()`'s
either/or early return (libcamera-only *or* cv2-only, never both) is
replaced with a union over both sources' keys, resolving each index
through the existing `resolve_identity()` fallback chain unchanged.
`resolve_identity()` itself needs no logic change — its existing
"check `libcamera_entries` first, else fall through to the cv2 chain"
order already implements loopback-wins dedup, once both sources share
one index domain.

### Why

Two bugs share one root cause: `identity.py` treats "does this host have
any libcamera loopback" as a boolean gate on the *entire* enumeration
result, rather than merging per-camera. The visible symptom (USB
cameras never registered on a mixed host) and a latent one (a real
camera's cv2 index can numerically collide with an unrelated loopback's
enumeration-order `position`, corrupting `OpenCVBackend`'s post-open
identity-mismatch check) both disappear once the two enumeration
sources are merged on a shared, physically-meaningful index domain
(the real `/dev/videoN` number) instead of on two independently-counted
small-integer sequences that happen to overlap.

### Impact on Existing Components

- `camera/discovery.py` — no change. It already treats
  `identity_source()`'s output as "whatever cameras are currently
  present," with no assumption that a host is all-libcamera or all-cv2;
  it will simply receive a dict with more entries on a mixed host.
- `camera/backends/opencv_backend.py`,
  `camera/backends/worker_backend.py` — no change. Both call
  `resolve_identity(index)` with a real cv2 index for post-open identity
  verification; re-keying `_probe_libcamera()` by device index removes
  the latent collision risk without touching either call site.
- `camera/backends/__init__.py` (`resolve_capture_backend`) — docstring
  fix only. It already ignores the `index` it's passed for a
  libcamera-sourced `hardware_key` and re-derives the real device index
  by matching `hardware_key`'s embedded `camera_id` against a fresh
  `list_loopback_cameras()` call, so passing a now-correct device index
  through `probe()` changes nothing functionally there.
- `camera/registry.py` — no change. It keys on `hardware_key`, which is
  unaffected by this fix; a USB camera simply starts arriving in the
  identities dict it reconciles against.

### Design Rationale

**Decision:** re-key `_probe_libcamera()`'s dict by `device_index`
rather than, e.g., prefixing libcamera keys (`f"lc:{position}"`) to make
them collision-proof by construction.

**Context:** the merge needs one dict whose keys `resolve_identity` can
look up unambiguously across two enumeration sources that previously
had no shared numbering.

**Alternatives considered:**
- *String-prefixed keys* (e.g. `f"lc:{position}"` vs. a plain `int` for
  cv2) — would require every consumer of the returned `{index:
  CameraIdentity}` dict (`discovery.py`'s `probe()`, `_probe_openable`,
  `resolve_capture_backend`) to branch on key type, and would still need
  `resolve_capture_backend` to re-derive the real device index for a
  `V4l2LoopbackBackend` anyway — it already does this via `hardware_key`
  matching, not via the passed index, so string keys would add
  complexity there for no benefit.
- *Keep `position`, dedup by scanning `enum_entries` for a path match
  against each `LibcameraDevice.device_index`* — works, but adds an
  O(n·m) cross-referencing step to every `resolve_all()` call and still
  leaves the latent `resolve_identity(index)` collision in
  `opencv_backend.py`/`worker_backend.py` unaddressed, since those call
  sites pass a bare int with no access to the loopback list for
  cross-referencing.

**Why this choice:** re-keying by `device_index` uses a domain that is
already physically unique (`/dev/videoN` cannot name two live devices at
once) and that `_enumerate_cv2()` already lives in (confirmed by
sprint 041's backend-offset stripping work) — so unifying on it is a
one-line change at the source (`_probe_libcamera`) that fixes both the
visible bug and the latent collision, everywhere a caller does an
integer-keyed lookup.

**Consequences:** `LibcameraDevice.position` remains a valid field
(still used for its documented "stable enumeration order" purpose
elsewhere), it's just no longer the key `identity.py` uses internally.
Any test fixture that builds a `_LibcameraEntry`/`_probe_libcamera`
stand-in keyed by position must be updated to key by device index
instead — scoped into ticket 001's test work.

### Migration Concerns

None. This is a pure bugfix with no data migration: the registry's
existing records (`imx296` #1, `imx296-2` #2) are addressed by
`hardware_key`, which this change does not alter for any camera already
registered. No config format change, no backward-compatibility concern.
Deployment sequencing for the live-verification ticket is: build wheel
→ `pipx install --force` as `aprilcam` user → verify new code is on
disk → restart `aprilcamd` (not before) → verify via `camera probe`/
`camera image` from the Mac.

## Use Cases

Compact sprint — one updated use case, refining UC-25's existing "probe
brings the camera list up to date" contract for a host with mixed
camera sources.

### SUC-001: Probe a Linux host with both CSI (libcamera) and USB cameras
Parent: UC-25

- **Actor**: Developer/operator running `aprilcam camera probe` (or the
  daemon's own passive watch) against a Raspberry Pi that has a working
  CSI camera bridged through `v4l2loopback` and a USB/UVC camera plugged
  in at the same time.
- **Preconditions**: Daemon running on the Pi (`vidar.local` in this
  sprint's live verification). At least one labelled `aprilcam-*`
  v4l2loopback device present, and at least one USB/UVC camera attached.
- **Main Flow**:
  1. Operator runs `aprilcam camera probe -d vidar.local`.
  2. The daemon re-enumerates both sources: `_probe_libcamera()` for
     loopback devices, `_enumerate_cv2()` for USB/UVC devices.
  3. `resolve_all()` resolves every physical camera exactly once: each
     loopback under its `libcamera:` identity, each USB/UVC camera under
     its normal fallback-chain identity — including the loopback device
     that also appears in the cv2 enumeration, which is not
     double-counted.
  4. Each identity is reconciled against the registry
     (`camera/registry.py`), preserving existing numbers for
     already-known cameras and assigning the next number to the new USB
     camera.
  5. The probe result lists all cameras, each once, with correct
     usable/unusable status.
- **Postconditions**: The registry holds one entry per physical camera.
  Previously-registered cameras (`imx296` #1, `imx296-2` #2) keep their
  numbers. The USB camera is registered under a new number and is
  capturable via `camera image`/`camera view`.
- **Acceptance Criteria**:
  - [ ] `camera probe -d vidar.local` lists `imx296`, `imx296-2`, and the
        USB camera, each exactly once.
  - [ ] `imx296` and `imx296-2` retain registry numbers #1 and #2.
  - [ ] `camera image -d vidar.local <usb-slug>` returns a real,
        inspectable frame.
  - [ ] `camera image` for `imx296` continues to work unchanged.
  - [ ] A loopback-only host and a UVC-only host (including macOS)
        resolve exactly as they did before this sprint.

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
| 001 | Merge libcamera and cv2 camera enumeration in identity.py | — |
| 002 | Update camera/DESIGN.md and knowledge doc for mixed-source resolution | 001 |
| 003 | Live verification on vidar: USB camera registers alongside CSI cameras | 001 |

Tickets execute serially in the order listed.
