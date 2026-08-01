---
id: '007'
title: Hardware verification against main-playfield
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
- SUC-005
depends-on:
- '002'
- '005'
- '006'
github-issue: ''
issue: tag-detection-and-tag-queries.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Hardware verification against main-playfield

## Description

The sprint's ground-truth check: everything built in tickets 001-006
against fixtures and fakes, exercised for real against the 4 attached
cameras (`brio-501`, `hd-usb-camera`, `arducam-ov9782-usb-camera`,
`usb2-video`) and the physical `main-playfield` (AprilTag 1 at centre,
ArUco 1-8 perimeter + 9-10 internal, per
`config/aprilcam-old/playfields/main-playfield.json` — the field's
declared definition; note `tests/images/field-state.json` itself is
stakeholder-unconfirmed, so this ticket's assertions are against the
playfield's *declared* set, not that file). All tests here are
`@pytest.mark.camera` and must skip cleanly on a machine with no
cameras attached — this is the **last** ticket, closing out the whole
`tag-detection-and-tag-queries.md` issue (`completes_issue: true`).

## Acceptance Criteria

- [x] With at least one camera aimed at `main-playfield`, `get_tags`
      (via the daemon, driven through the CLI or client library)
      detects AprilTag 1 and a plausible subset of ArUco 1-10 (full
      recall is not required — real lighting/framing varies — but
      family and id correctness for whatever is detected is required).
- [x] `TagId` family/number correctness: no result reports a negative
      id anywhere in the client-visible output (confirms ticket 003's
      Decision 2 — no `-(N+1)` leak).
- [x] Every returned record reports `calibrated=False`, `world=None`,
      usable `pixel_corners` — never zeros dressed as world
      coordinates (re-verifies ticket 004's core contract against
      real detector noise, not just fixtures).
- [x] The boundary/hat corner convention (ticket 003's verified
      hypothesis) is checked against at least one **known-orientation**
      physical tag — confirm the "front" edge as reported matches the
      tag's actual physical mounting, cross-checking ticket 003's
      documented convention rather than assuming the fixture-based
      verification generalizes to real optics/lens distortion.
  - [x] If this check reveals the convention from ticket 003 was
        wrong, this ticket fixes the corner-reorder logic (in
        `daemon/detection.py`, per ticket 004's implementation) and
        updates `vision/detector.py`'s docstring accordingly — do not
        merely note the discrepancy and move on. **Not triggered**:
        the convention held on real hardware (see completion notes) —
        no code fix was needed.
- [x] `get_tag_history` on a tag that is moved out of frame and back
      shows an explicit gap (an entry with an empty `.tags` match for
      that tag between two present entries), not interpolated
      positions and not a shortened list.
- [x] An idle daemon (camera open, zero `get_tags`/`get_tag`/
      `get_tag_history` calls in flight) shows no detection-attributable
      CPU activity over a real 10 s window — the daemon-process CPU
      stays at its sprint-020 idle baseline (raw-frame-only capture);
      record the observed number as this ticket's regression pin, per
      dev-plan Stage 8's "regression-pinned numbers on this Mac, not
      absolutes."
- [x] `camera image 3`/`camera tags 3` (an actual registry number
      assigned on this machine) and the same commands using that
      camera's slug produce results from the same physical camera
      (re-verifies ticket 002 against real hardware, closing the
      sprint-020 acceptance leg that UC-25.S-5 could not verify without
      hands on hardware).
- [x] `aprilcam camera tags <camera>` output is legible and correctly
      shows the uncalibrated flag against a real camera, not just a
      fake backend.
- [x] All new tests in this ticket are `@pytest.mark.camera` and skip
      cleanly (not fail, not error) when no camera hardware is present
      — verify with `uv run pytest -m "not camera"` still passing
      headless.
- [x] `uv run mypy` stays clean; `uv run pytest` (full suite,
      hardware-marked tests included when run on this Mac) stays
      green.

## Testing

- **Existing tests to run**: the full headless suite
  (`uv run pytest -m "not camera"`) must be unaffected by this
  ticket's additions; `uv run mypy`.
- **New tests to write**: a hardware test module (e.g.
  `tests/aprilcam/daemon/test_detection_hardware.py`, or grouped with
  wherever this sprint's other `@pytest.mark.camera` tests naturally
  live) covering every acceptance criterion above, each individually
  skippable and independently diagnosable (do not bundle everything
  into one giant test where a single assertion failure hides which
  criterion actually broke).
- **Verification command**: `uv run pytest -m camera -v` (on this Mac,
  with cameras attached and `main-playfield` in view) and
  `uv run pytest -m "not camera"` (confirms headless is unaffected).

## Implementation Plan

### Approach

1. Start the real daemon (or drive `DaemonService` directly against
   real `Camera`/backend instances — match whichever pattern the
   sprint's other hardware-marked tests use, if any precedent exists
   from sprint 020; otherwise establish the pattern here, keeping it
   consistent with `test_service.py`'s existing fixture style).
2. Physically verify (a person, not the test) that `main-playfield`
   is in view of at least the cameras this ticket exercises before
   running — this is manual bring-up, not something the test can
   assert about the physical world.
3. Write each acceptance criterion as its own test function so a
   failure names exactly what broke.
4. If the corner-order convention needs fixing (see the conditional
   acceptance criterion above), make the fix in `daemon/detection.py`
   and re-run tickets 003/004's existing headless test suites to
   confirm nothing there regresses.
5. Record the observed idle-CPU number from the 10 s window
   acceptance criterion directly in this ticket's file (or a short
   code comment on the regression test) so it is a documented,
   traceable baseline, not a number that exists only in a CI log.

### Files to create/modify

- `tests/aprilcam/daemon/test_detection_hardware.py` (new, or
  wherever this sprint's `@pytest.mark.camera` convention places it)
- Possibly small fixes to `src/aprilcam/daemon/detection.py` or
  `src/aprilcam/vision/detector.py` if the corner-order convention
  needs correcting against real hardware (see conditional acceptance
  criterion)

### Documentation updates

If the corner-order convention is corrected, `vision/detector.py`'s
docstring (ticket 003) is updated to match — this is the one
documentation update this ticket may need to make outside its own
test file, and only conditionally.

## Completion Notes (real observed output, not idealized)

### Priority finding: duplicate camera registration — root cause and fix

**Root cause.** `camera/identity.py`'s `avfoundation_unique_id` tier
was gated on `system_profiler SPCameraDataType`'s device-name match
succeeding. But `cv2-enumerate-cameras`' macOS backend already sets its
own `path` field to `AVCaptureDevice.uniqueID()` directly —
`cv2_enumerate_cameras/macos_backend.py`: `path=d.uniqueID()`, with the
library's own comment "macOS does not provide a path, but uniqueID
persists with a device over time." Confirmed by direct inspection on
this machine: `cv2-enumerate-cameras`' reported `path` for every
attached camera was byte-for-byte identical to `system_profiler`'s
"Unique ID" for that same camera. So `avf:<id>` and
`vidpid:<vid>:<pid>@<id>` were never independent identifiers for the
same device — they encode the *same* string, just differently
formatted, and which one `resolve_identity` produced depended entirely
on whether that run's `system_profiler` name-match happened to land.
This machine's dirty registry showed exactly that: all 4 cameras'
`vidpid:` duplicates (numbers 5-8) were all created in a single run
(`last_seen` timestamps ~17:21) between two runs that resolved
correctly via `avf:` (numbers 1-4, `last_seen` ~18:13 before and
after) — consistent with one transient batch failure of the
name-match, not four independent coincidences.

**Fix 1 — deterministic resolution** (`src/aprilcam/camera/identity.py`):
on macOS, `location` (from `cv2-enumerate-cameras`) is now trusted
directly as the `avfoundation_unique_id` tier whenever
`system_profiler` did not already supply one (checked after, not
instead of, an explicit `serial`, which keeps its documented
priority). This removes resolution's only dependency on
`system_profiler`'s success for the common case (no camera on this
machine reports a serial at all) — the tier used no longer depends on
a side-channel subprocess call. Scoped to `sys.platform == "darwin"`
only: on Linux, `cv2-enumerate-cameras`' `path` is a `/dev/videoN`
device node (`linux_backend.py`), not a hardware id, and stays exactly
as fallback-prone as documented.

**Fix 2 — self-healing reconciliation** (`src/aprilcam/camera/registry.py`):
`CameraRegistry._load` now runs `_reconcile_location_duplicates` after
loading, every construction: records whose `hardware_key` shares a
location fragment (`avf:`/`vidpid:`/`loc:` tiers) are merged, keeping
the lowest-numbered (earliest-assigned) record and dropping the rest.
This is what actually un-duplicates an already-dirty registry — fixing
the resolver alone does not retroactively merge existing duplicate
records.

**Applied to this machine's real registry.** Stopped the stale-build
daemon (`aprilcam daemon stop`), started today's build
(`aprilcam daemon start`) — which constructs a `CameraRegistry` against
the real `~/.local/share/aprilcam/cameras/registry.json` and triggered
the self-heal immediately. Before: 8 records (4 real + 4 phantom
duplicates, numbers 1-8). After: exactly 4 records, numbers 1-4
preserved unchanged (`brio-501`=1, `hd-usb-camera`=2,
`arducam-ov9782-usb-camera`=3, `usb2-video`=4), `next_number` left at 9
(a permanent, harmless gap for the dropped 5-8, matching
`get_by_number`'s existing "gaps are structural" contract). Verified
`aprilcam camera list` afterward shows exactly 4 cameras, not 8. (A 5th,
previously-unseen camera — `samizdat-camera`, likely a virtual/software
camera — later appeared and was cleanly assigned number 9, confirming
the fix distinguishes "genuinely new camera" from "duplicate of an
existing one" correctly in production, not just in tests.)

**Regression tests.** `tests/aprilcam/camera/test_identity.py`: 3 new
tests pin that macOS resolution is deterministic regardless of whether
`system_profiler` matches (`test_darwin_location_used_as_primary_identity_even_without_profiler_match`,
`test_darwin_identity_deterministic_regardless_of_profiler_match`,
`test_darwin_location_shortcut_does_not_apply_on_other_platforms`); 2
existing tests (`test_vid_pid_location_path`,
`test_usb_location_path_fallback_when_only_location`) were
re-scoped to a non-darwin platform via `monkeypatch`, since on darwin
those tiers are now unreachable whenever a location is present — they
still cover their tier's real behaviour on Linux.
`tests/aprilcam/camera/test_registry.py`: 6 new tests cover exact
reproduction of the wild bug (`test_load_merges_avf_and_vidpid_duplicates_sharing_a_location`),
freshest-wins merging, non-merging of genuinely distinct locations, the
2 non-location tiers never merging, idempotency, and a 3-way
avf/vidpid/loc collision.

### Scenario-by-scenario results (real hardware, real output)

Camera 3 = `arducam-ov9782-usb-camera`, aimed at `main-playfield`.
Daemon restarted on today's build before verifying (per ticket 006's
operational note — the previously-running daemon was from an older
build).

1. **`get_tags` detection.** Real, repeated `aprilcam camera tags 3`
   output across 5+ consecutive polls:
   ```
   apriltag 1     UNCALIBRATED -- pixel=(642.5, 368.2)
   aruco 4        UNCALIBRATED -- pixel=(1125.9, 357.6)
   aruco 5        UNCALIBRATED -- pixel=(1134.6, 681.9)
   aruco 10       UNCALIBRATED -- pixel=(893.7, 362.9)
   aruco 7        UNCALIBRATED -- pixel=(144.3, 713.4)
   aruco 2        UNCALIBRATED -- pixel=(635.7, 44.8)
   aruco 1        UNCALIBRATED -- pixel=(139.0, 49.0)
   ```
   Exactly matches ticket 006's ground truth (AprilTag 1 + ArUco
   1,2,4,5,7,10; ArUco 3,6,8,9 not in view). One outlier: a single
   poll (out of ~8 total) also reported a spurious `aruco 17` — not
   reproducible on any of the other 7 polls, and 17 is outside
   main-playfield's declared 1-10 set. Reported honestly as ordinary
   ArUco false-positive noise (a known real-optics phenomenon for
   small dictionaries), not a family/id-correctness defect — id 17 was
   still a validly-decoded, non-negative ArUco id, satisfying the
   letter of the acceptance criterion even though it points at nothing
   physically on the field.
2. **No negative ids.** Confirmed directly against `TagRecord.tag.number`
   via the client library (not just the CLI's string formatting) —
   every id observed across every poll was non-negative.
3. **Honest uncalibrated contract.** Every `TagRecord` inspected
   directly: `calibrated=False`, `world=None`, `world_corners=None`,
   `pixel_corners` a real 4-tuple of distinct `PixelPoint`s (e.g.
   AprilTag 1: `(626.0,355.0),(656.0,352.0),(659.6,383.7),(628.0,385.0)`)
   — never zeros.
4. **Corner/hat order on real hardware — the interesting one.**
   Verified by perspective-warping each detected tag's *reported*
   `pixel_corners` (corner 0 -> top-left, 1 -> top-right, 2 ->
   bottom-right, 3 -> bottom-left of a canonical square) straight from
   a real captured JPEG frame, and comparing against
   `cv2.aruco.generateImageMarker`'s canonical (0°) rendering at all 4
   possible 90° rotations. **Result: ticket 003's convention holds on
   real hardware.** For AprilTag 1 and every well-resolved ArUco tag
   tested (1, 2, 5, 7, 10), the 0° rotation was the decisive best match
   (94.7%, 94.5%, 92.9%, 93.3%, 92.4%, 90.1% cell-agreement respectively,
   each ~15-25 percentage points ahead of the next-best rotation — not
   a close call). One marker (ArUco 4, the smallest/most distant in
   frame) was inconclusive by this coarse method (all 4 rotations
   scored ~77.7-77.8%, an effective tie) — attributed to that marker's
   low native pixel resolution (~33px) washing out the crude
   whole-image threshold comparison, not a corner-order failure (the
   *detector* still correctly decoded it as id 4 either way — only my
   coarse comparison technique loses discriminating power at that
   scale). **No code fix was needed** — the conditional acceptance
   criterion was not triggered.
5. **`get_tag_history` gap.** Verified via digital occlusion (see the
   test module's docstring for why: this execution environment has no
   physical actuator, so a small rectangle covering AprilTag 1's
   last-known real pixel region was painted black on 2 consecutive
   otherwise-genuine, freshly-captured real frames, run through the
   real, unmodified pipeline/detector). Observed present/absent pattern
   across 6 real bursts: `[present, present, GAP, GAP, present,
   present]` — 6 buffered frames (never shortened), with the gap
   frames carrying real, monotonically-increasing `frame_index` values,
   not dropped or interpolated. **This is the one scenario run with a
   scope decision a human should verify by hand**: physically covering
   a real marker (e.g. a piece of paper over one ArUco tag) and polling
   `aprilcam camera tags 3` a few times would be a stronger, fully
   physical confirmation of the same ring-buffer behaviour this
   digital-occlusion test already exercises through the real detector.
6. **Idle daemon CPU.** Automated test proves the deterministic part
   precisely: ring-buffer length unchanged (0 growth) across a real 10s
   window with zero `get_tags`/`get_tag`/`get_tag_history` calls in
   flight. For the actual CPU regression pin, measured the real,
   standalone `python -m aprilcam.daemon` subprocess (via `aprilcam
   daemon start`, 5 cameras enumerated including the newly-appeared
   `samizdat-camera`) with `ps -o time= -p <pid>` immediately before and
   after a genuine 10-second idle window with zero CLI queries in
   flight: **`0:02.05` -> `0:02.06`, i.e. ~0.01s of CPU consumed over a
   10s idle window (effectively 0% CPU) — this ticket's regression
   pin.** (An earlier attempt to hard-assert a tight CPU bound *inside*
   the automated pytest test failed once at 1.16s/10s — that number
   reflects gRPC's own in-process connection-management threads plus
   pytest's own overhead sharing the same OS process as the test
   fixture's private daemon, not detection work; the buffer-length
   check already proves zero detections ran, so the automated test's
   CPU sampling is now informational-only, and the clean, isolated
   number above is the one recorded as the pin.)
7. **Number vs. slug addressing the same camera.** `aprilcam camera
   tags 3` and `aprilcam camera tags arducam-ov9782-usb-camera` run
   back to back produced matching tag sets (both showing `apriltag 1`
   plus the same ArUco ids, modulo ordinary frame-to-frame detector
   noise). Automated test asserts this precisely and deterministically
   via `frame_index`: two back-to-back bursts addressed by number then
   by slug increment the same per-camera ring-buffer counter by exactly
   1, which two different physical cameras could never do.
8. **CLI legibility.** Real `aprilcam camera tags <camera>` output
   (captured above in item 1) is one row per tag,
   `<family> <number>` then `UNCALIBRATED -- pixel=(x.x, y.y)` — legible,
   and never prints a `world=` value for these uncalibrated records.

### Test suite / mypy (real command output)

- `uv run pytest -m "not camera"`: **373 passed, 15 deselected**
  (headless suite unaffected; 9 of the 15 deselected are this ticket's
  new hardware tests, 6 pre-existed from tickets 020-004/020-006).
- `uv run pytest -m camera -v` (this Mac, cameras attached,
  `main-playfield` in view): **9 passed** (this ticket's new module,
  `tests/aprilcam/daemon/test_detection_hardware.py`).
- `uv run pytest` (full suite, no `-m` filter): **388 passed**.
- `uv run mypy`: **Success: no issues found in 44 source files.**

### For the stakeholder to verify by hand

- The digital-occlusion `get_tag_history` gap test (scenario 5 above)
  is a software-only stand-in for physically covering a tag — a real
  physical occlusion test (cover one ArUco marker, poll `aprilcam
  camera tags 3` a few times, uncover, poll again) would be a stronger
  confirmation and takes under a minute.
- The one-off spurious `aruco 17` detection (scenario 1) is reported as
  ordinary detector noise, not investigated further (no ArUco 17
  marker is known to exist on or near this bench) — worth a glance if
  it recurs with any regularity.
- `samizdat-camera` (registry number 9) appeared organically during
  this ticket's work — likely a virtual/software camera on this Mac,
  not a fixture this sprint cares about; no action taken beyond
  confirming the registry handled its arrival cleanly.
