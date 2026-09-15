---
id: '041'
title: Filter Pi ISP internal nodes out of camera enumeration
status: executing
branch: sprint/041-filter-pi-isp-internal-nodes-out-of-camera-enumeration
use-cases:
- SUC-001
- SUC-002
issues:
- linux-enumeration-lists-pi-isp-nodes-as-cameras-with-duplicate-rows.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 041: Filter Pi ISP internal nodes out of camera enumeration

## Goals

Make Linux camera enumeration report exactly the real, standalone cameras
on a host — once each — and stop the daemon from ever opening or
continuously probing Raspberry Pi RP1/PiSP internal V4L2 nodes. Clean up
registry rows already created for those nodes without hand-editing state
files.

## Problem

On a Pi 5 (`vidar.local`), `aprilcam camera probe` lists ten RP1/PiSP
internal V4L2 nodes (`rp1-cfe-fe-image0/1`, `rp1-cfe-csi2-ch0/2/3`, and
their `-2` CSI-port variants) as if they were cameras, and prints every
row — junk and real — twice. Those junk nodes also get persistent
registry numbers and linger after the real cause (a missing
v4l2loopback module) is fixed. Probing them with OpenCV is also actively
harmful: the daemon opens them repeatedly, producing a continuous
`node link is not enabled` dmesg stream on both CSI controllers, because
raw PiSP/CFE nodes belong to a libcamera-owned media pipeline and are
not meant to be opened directly.

Root-cause analysis (confirmed by reading the vendored
`cv2_enumerate_cameras` source, not by inference):

- **Duplicate rows.** `cv2_enumerate_cameras.enumerate_cameras()`
  (called with no argument, i.e. `apiPreference=CAP_ANY`, exactly how
  `camera/identity.py::_enumerate_cv2` calls it) iterates
  `supported_backends` and yields one `CameraInfo` per backend per
  device, combining `index = real_index + backend_id` to keep the
  per-backend entries distinct. On macOS `supported_backends =
  (CAP_AVFOUNDATION,)` — one backend, so this is invisible, and
  `_enumerate_cv2`'s existing `idx = raw - avf_offset if raw >=
  avf_offset else raw` correctly un-offsets it. On Linux,
  `linux_backend.supported_backends = (CAP_GSTREAMER=1800,
  CAP_V4L2=200)` — **two** backends over the identical `/dev/video*`
  glob, so every real `/dev/videoN` device (junk nodes included) is
  yielded twice, at two different combined indices neither of which
  equals the real device index. `_enumerate_cv2`'s offset-stripping
  logic only knows about the AVFoundation case, so on Linux both
  entries survive as apparently-distinct cameras instead of collapsing
  back to one — this is the entire "every row printed twice" bug, and
  it affects real cameras and junk nodes identically. It is a
  Linux-wide bug, not Pi-specific: any USB/UVC camera on a Linux host
  would show the same duplication.
- **Junk nodes admitted at all.** `cv2_enumerate_cameras`'s own
  admission check (`device_can_capture_video`, a `VIDIOC_QUERYCAP`
  `V4L2_CAP_VIDEO_CAPTURE` check) does not exclude the RP1 nodes,
  because they genuinely are V4L2 video-capture-capable devices from
  the kernel's point of view — that capability bit is how libcamera
  itself captures off them. Excluding them requires a test this
  vendored dependency does not perform: v1
  (`archive/src/aprilcam/camera/camutil.py::_list_v4l2_usb_cameras`)
  already solved exactly this by reading the `/sys/class/video4linux/
  videoN/device/driver` symlink and keeping only nodes whose driver is
  a real capture-source driver (`uvcvideo`), which incidentally also
  excludes every RP1/PiSP/codec node without needing to name them.

## Solution

1. Fix `camera/identity.py::_enumerate_cv2`'s index recovery so it
   strips whichever Linux backend offset (`CAP_V4L2` or
   `CAP_GSTREAMER`) actually produced a given entry, the same way it
   already strips `CAP_AVFOUNDATION` on macOS — collapsing the two
   backend-tagged entries cv2-enumerate-cameras yields per physical
   `/dev/videoN` back into one dict entry keyed by the real device
   index. This is the fix for "every row printed twice," on Linux
   generally, not just on a Pi.
2. Add a Linux-only node classifier to `camera/identity.py`, applied
   before a `/dev/videoN` candidate becomes an enumeration entry:
   principled test first — the node's V4L2 driver name (read via the
   `/sys/class/video4linux/videoN/device/driver` symlink, mirroring
   v1's proven approach) must not be one of the known Pi
   media-pipeline-internal drivers (`rp1-cfe`, `pispbe`, `rpivid`,
   `bcm2835-codec`); a narrow, documented name-prefix backstop
   (`rp1-cfe-`, `pispbe-`) covers the case where the driver symlink is
   unreadable. A driver read failure or an unrecognized driver *keeps*
   the node (fail open) — this must never cause a real USB camera or
   an unfamiliar future driver to be silently dropped. Because
   enumeration never proposes these nodes as candidates, the daemon
   never resolves an identity for them, never registers them, and
   never opens or probes them — which is also what stops the dmesg
   spam.
3. Purge already-registered junk rows without adding any new
   cross-module dependency: `camera/discovery.py`'s `CameraDiscovery`
   already imports both `camera/identity.py` and `camera/registry.py`.
   In `probe()`'s existing "registry record not seen in this
   enumeration pass" branch, reuse identity's node classifier against
   the stored record's `device_name`; a match calls the registry's
   existing `forget(slug)` (already public, already used elsewhere)
   instead of reporting the record `present=False`. `probe()` already
   runs once at daemon startup (`daemon/__main__.py`) before `watch()`
   starts, so a plain `aprilcamd` restart is enough to clear vidar's
   stale rows — no manual registry edit, no new module, no new
   dependency edge.

## Success Criteria

- `aprilcam camera probe -d vidar.local` lists only `imx296` and
  `imx296-2`, each exactly once.
- vidar's dmesg stops accumulating `node link is not enabled` lines.
- A frame still comes from `imx296-2`; `imx296` (known hardware-faulty)
  still appears, once, unusable.
- The registry's `[3]..[12]` junk rows are gone after an `aprilcamd`
  restart, without hand-editing `registry.json`.
- `imx296` keeps registry number 1 and `imx296-2` keeps number 2 —
  purging junk rows never renumbers real cameras.
- Offline tests (fake `/sys` trees, fake `cv2_enumerate_cameras`
  results) cover the dedup fix, the node classifier, and the registry
  purge, and pass under `scripts/test-gate.sh`.

## Scope

### In Scope

- `camera/identity.py`: the Linux backend-offset dedup fix and the new
  node classifier.
- `camera/discovery.py`: reusing the classifier in `probe()`'s absent-
  record branch to purge instead of report.
- Offline unit tests for both, covering Linux, macOS, and non-Pi Linux
  (USB/UVC) behavior.
- Live verification and deployment to vidar.

### Out of Scope

- Any change to `camera/registry.py` itself (no new method, no schema
  change — `forget()` already exists and is reused as-is).
- A real libcamera capture backend, media-controller graph introspection,
  or any change to the `aprilcam-*` loopback path (sprint 020's
  libcamera bridge is unaffected — it never calls `_enumerate_cv2`).
- Windows support (`cv2_enumerate_cameras.windows_backend` is untouched;
  out of this project's target platforms).
- Any change to `camera list`/`probe` CLI rendering — the duplication
  and junk rows are fixed at the enumeration source, not papered over
  in `cli/camera.py` or `cli/_common.py::print_probe_report`.

## Test Strategy

Offline only (`-m "not camera and not integration"` /
`scripts/test-gate.sh`), using fake `/sys/class/video4linux` trees and
injected fake `cv2_enumerate_cameras`-shaped results — the same
injection-point pattern `identity.py`'s existing tests already use
(`enum_entries`, `profiler_entries`, `libcamera_entries` overrides) —
plus a fake registry.json fixture for the purge test. Coverage:

- Linux dedup: two backend-tagged entries for the same `/dev/videoN`
  collapse to one `CameraIdentity`.
- Node classifier: `rp1-cfe-*`/`pispbe-*`/`rpivid` nodes (by driver
  symlink and by name-prefix backstop) are excluded; a real USB/UVC
  camera (`uvcvideo` driver) and an unrecognized/unreadable-driver node
  are kept (fail open).
- macOS path unaffected: `_macos_camera_profiles`/AVFoundation offset
  logic untouched, single-backend case still resolves correctly.
- Sprint-040 loopback path unaffected: when `list_loopback_cameras()`
  returns entries, `resolve_all()` still short-circuits before
  `_enumerate_cv2` is ever called.
- Registry purge: a registry fixture with junk + real records purges
  only the junk ones on the next `probe()`, preserving the real
  cameras' numbers.
- Live verification on vidar (ticket 003, manual/system-level, not part
  of the offline gate).

## Architecture

**Compact.** This sprint changes the internals of one existing
component — camera enumeration/discovery in `src/aprilcam/camera/` —
fixing a bug and adding a filtering predicate. It introduces no new
module, no new cross-module dependency (the one place a fix reuses
another module's function, `camera/discovery.py` reusing
`camera/identity.py`, is an edge that already exists — `discovery.py`
has imported `identity.py` since ticket 002), no dependency-direction
change, and no data-model change (`CameraRecord`'s shape is unchanged;
`forget()` is an existing method, not a new one). No diagram is
included: there is nothing new being composed for a component diagram
to show, and the two touched functions already sit inside one
component whose relationship to its neighbors (`registry.py`,
`backends/`) is unchanged.

### What Changed

- **`camera/identity.py`** — `_enumerate_cv2()`'s combined-index
  recovery is extended from "strip `CAP_AVFOUNDATION` if present" to
  "strip whichever of `CAP_AVFOUNDATION`, `CAP_GSTREAMER`, `CAP_V4L2`
  produced this entry," fixing the Linux-wide duplicate-row bug. A new
  module-private predicate (name TBD by the implementing ticket, e.g.
  `_is_internal_pi_node(name, path)`) is added and consulted inside the
  Linux enumeration loop before a `/dev/videoN` candidate is turned into
  an `_EnumEntry`; nodes it rejects never enter `_enumerate_cv2()`'s
  output dict at all.
- **`camera/discovery.py`** — `CameraDiscovery.probe()`'s existing loop
  over `self._registry.records()` (the "not seen in this enumeration
  pass" branch) gains one branch: before appending a `present=False`
  `CameraState`, check the stored record's `device_name` against
  identity's node predicate; on a match, call
  `self._registry.forget(record.slug)` and do not add it to the report
  at all.

### Impact on Existing Components

- **`camera/registry.py`** — none. `forget()` is called, not changed.
  The self-heal-on-load pattern already established there
  (`_reconcile_location_duplicates`) is left as prior art / a template,
  not touched or extended.
- **`camera/backends/libcamera.py`** and the sprint-040 loopback path —
  none. `list_loopback_cameras()` and `_probe_libcamera()` are
  unaffected; `resolve_all()`'s early return when libcamera entries
  exist means `_enumerate_cv2()` is not even called on a Pi with
  working loopbacks.
- **macOS / `_macos_camera_profiles`** — none. The node predicate is
  gated to platforms where it is meaningful (Linux); macOS keeps
  exactly its current single-backend offset handling.
- **`daemon/service.py`**, **CLI (`cli/camera.py`)** — none directly;
  both benefit because `list_cameras()` reads `registry.records()`
  straight through, so a purged record disappears from `camera list`
  the moment `probe()` (already run once at daemon startup) removes it
  from the registry — no CLI or daemon-service code changes needed.

### Design Rationale

**Decision: filter at enumeration, purge via the existing
discovery→identity/registry edges, add no new module.**
*Context*: junk nodes need to be kept out of *future* enumeration, and
rows already written to `registry.json` on affected hosts need to
disappear without a manual edit. *Alternatives considered*: (a) give
`camera/registry.py` its own self-heal step (mirroring
`_reconcile_location_duplicates`) that imports the new predicate from
`identity.py` directly; (b) put the predicate in a new shared
`camera/node_filter.py` module imported by both `identity.py` and
`registry.py`; (c) do the purge in `discovery.py`, which already
depends on both modules. *Why this choice*: (a) and (b) each add a
dependency edge that does not exist today (`registry.py` importing
from `identity.py`, or both importing a new module) — a real, if small,
coupling change that would push this sprint into the substantial tier
for no benefit. (c) uses two edges `discovery.py` already has,
so the whole fix stays a same-tier, same-shape change to logic already
living in the one component that owns "decide what a probe reports."
*Consequences*: the node predicate's natural home is `identity.py`
(where enumeration decisions already live) and `discovery.py` imports
one more name from a module it already imports from — a one-line import
change, not a new edge.

**Decision: driver-name check as the principled test, name-prefix as
backstop only.** *Context*: the evidence shows the vendored
`cv2_enumerate_cameras`'s own `V4L2_CAP_VIDEO_CAPTURE` check cannot
distinguish an RP1 CFE/PiSP node from a real camera — both report that
capability. *Alternatives considered*: a pure name-prefix blocklist
(`rp1-cfe-`, `pispbe-`, `rpivid`); media-controller topology
introspection (walking `/dev/media*` graphs to detect a node owned by a
libcamera pipeline). *Why this choice*: v1
(`camutil.py::_list_v4l2_usb_cameras`) already proved the driver-name
check (`uvcvideo`) works and is simple; inverting it (excluding known
internal drivers rather than allow-listing `uvcvideo` alone) keeps
real, not-yet-seen capture hardware (a future USB camera under a
different driver) working without a code change, at the cost of
needing to name the internal drivers to exclude. Full media-controller
introspection was rejected as solving a more general problem than this
sprint needs, at meaningfully more implementation and test cost.
*Consequences*: a driver renamed by a future kernel/RPi-firmware update
could re-admit a junk node; the name-prefix backstop and the "fail
open, never fail closed on the unknown" rule bound the blast radius of
that risk to "the bug returns," never to "a real camera disappears."

**Decision: fail open, not closed, on classifier uncertainty.**
*Context*: the classifier runs on every Linux host, including ones this
sprint's authors never tested against. *Why*: a false negative
(re-admitting one Pi-internal node) reproduces today's bug at worst; a
false positive (excluding a real, unrecognized camera) would silently
break camera discovery for a user with no diagnostic short of reading
this code. The asymmetry of those two failure costs is why every branch
of the predicate that cannot positively confirm "this is a known
internal node" defaults to keeping the candidate.

### Migration Concerns

- **Registry data**: no schema change. Existing junk rows on vidar (and
  any other affected host) purge themselves the next time `probe()`
  runs — which happens automatically at daemon startup
  (`daemon/__main__.py`) — so deploying the fix and restarting
  `aprilcamd` is the entire migration step; no script, no manual
  `registry.json` edit.
- **Numbering**: `forget()` does not renumber survivors and does not
  reissue the freed number (`registry.py`'s own documented "gaps are
  structural, not an error" contract) — `imx296`/`imx296-2` keep
  numbers 1/2 regardless of how many junk rows above or below them get
  purged.
- **Deployment sequencing**: build → pipx install on vidar → restart
  `aprilcamd` (ticket 003's runbook). No coordination with other hosts
  or services is required; the fix is inert everywhere the bug did not
  reproduce (macOS, Pi with working loopbacks, non-Pi Linux with no
  internal nodes to exclude).

## Use Cases

Compact: two brief use cases updating UC-25 (Camera Discovery)'s
enumeration and reporting behavior on Linux.

### SUC-001: Linux enumeration reports only real, standalone cameras, once each
Parent: UC-25

- **Actor**: A user or agent running `aprilcam camera list`/`camera
  probe` against a daemon on a Linux host (Pi or otherwise).
- **Preconditions**: The daemon is enumerating cameras via
  `cv2-enumerate-cameras` (i.e. no working `aprilcam-*` v4l2loopback
  entries are already satisfying the request via the libcamera path).
- **Main Flow**:
  1. The daemon enumerates `/dev/video*` nodes.
  2. Enumeration excludes nodes belonging to a libcamera-owned media
     pipeline (RP1 CFE/PiSP internal nodes, codec nodes) via the driver
     check, with the name-prefix backstop.
  3. Enumeration collapses the V4L2/GStreamer backend-duplicated entries
     for each surviving physical device back into one record.
  4. The daemon never opens or probes an excluded node.
- **Postconditions**: `camera list`/`camera probe` show each real camera
  exactly once; no RP1/PiSP/codec node appears; no repeated "node link
  is not enabled" dmesg spam from probing them.
- **Acceptance Criteria**:
  - [ ] On vidar, `camera probe` lists only `imx296` and `imx296-2`,
        each once.
  - [ ] A real USB/UVC camera on a non-Pi Linux host still enumerates,
        once, unaffected by the new filter.
  - [ ] macOS enumeration is unaffected (no duplication existed there;
        none is introduced).

### SUC-002: Stale junk registry rows self-purge without manual editing
Parent: UC-25

- **Actor**: The daemon, at startup or on-demand probe.
- **Preconditions**: `registry.json` already holds records for nodes
  that the fixed enumeration (SUC-001) would no longer propose — e.g.
  vidar's `[3]..[12]` rows, created before this sprint's fix shipped
  while the v4l2loopback module was unbuilt.
- **Main Flow**:
  1. `probe()` runs (daemon startup, or an on-demand `camera probe`).
  2. For each registry record no longer seen in the current
     enumeration, the daemon checks whether the record's stored device
     name matches the internal-node classifier.
  3. A match is forgotten from the registry outright, instead of being
     reported as an absent camera.
  4. A non-matching absent record (a real camera that is genuinely
     unplugged) is still reported `present=False`, unchanged from
     today.
- **Postconditions**: Junk rows are gone from `registry.json` and from
  `camera list`/`camera probe` output; real cameras' numbers are
  untouched.
- **Acceptance Criteria**:
  - [ ] Restarting `aprilcamd` on vidar removes the `[3]..[12]` rows
        from `registry.json` with no manual edit.
  - [ ] `imx296` keeps number 1 and `imx296-2` keeps number 2 after the
        purge.
  - [ ] A genuinely unplugged real camera (not matching the internal-
        node classifier) still reports `present=False` rather than
        being forgotten.

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
| 001 | Fix Linux backend-offset dedup and filter internal Pi V4L2 nodes in camera/identity.py | — |
| 002 | Purge stale internal-node registry rows in CameraDiscovery.probe() | 001 |
| 003 | Deploy and verify camera enumeration fix on vidar | 001, 002 |

Tickets execute serially in the order listed.
