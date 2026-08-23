---
id: '004'
title: Operator-assisted image-matrix capture session for cameras 2, 3, 5
status: open
use-cases: [UC-10]
depends-on: ['003']
github-issue: ''
issue: aprilcam-v2-layered-test-system.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Operator-assisted image-matrix capture session for cameras 2, 3, 5

## Description

**This ticket requires a human physically present at the rig and
cannot be completed by an agent alone.** It is the sprint's one hard
human dependency — every ticket in ticket-group 1 and the
capture-tool-update half of ticket-group 2 (ticket 003) is sequenced
before this one specifically to minimize what blocks on it. Everything
downstream (tickets 005-008, the entire L0 pytest package) is gated on
this ticket landing successfully.

Using the updated capture tool from ticket 003, an operator captures a
fresh, confirmed image-matrix fixture generation (`fs-20260823`) for
cameras 2 (`hd-usb-camera`), 3 (`arducam-ov9782-usb-camera`), and 5
(`arducam-ov9281-usb-camera`), and confirms the field state that was
physically present during capture. The existing July corpus
(`unverified-20260729`) is left in place, not replaced — manifests
already carry `field_state_id`, so both generations coexist.

**Required real-world steps (agent cannot perform these):**
1. Stop the real daemon (`aprilcam daemon stop`) and close any open
   `aprilcam view` windows — the capture tool refuses to run while the
   daemon holds a device.
2. Run the capture tool from a Terminal session (not from an IDE/agent
   shell) — macOS camera permission for a fresh process requires this.
3. Physically confirm what is on the field at capture time: AprilTags
   present, all 10 ArUco markers present, which colored
   rectangles/dots are placed — and report this back so it can be
   written into `field-state.json`'s `fs-20260823` entry.
4. Note camera-5 lighting conditions at capture time (day/night) in the
   manifest, since camera 5's exposure sweep spans both.
5. Confirm the session completed cleanly (baseline controls restored,
   no camera left misconfigured) before ending the Terminal session.

## Acceptance Criteria

- [ ] `tests/images/fs-20260823/<slug>/` exists for all three cameras
      (2, 3, 5), each with a full variation-matrix set of JPEGs, a
      `manifest.json`, and a pinned `calibration.json` snapshot (per
      ticket 003's schema).
- [ ] `tests/images/field-state.json` gains a `fs-20260823` entry with
      `status: "confirmed"` and non-empty `visible_april_tags`/
      `visible_aruco_tags`/`visible_rectangles`/`visible_dots` arrays,
      populated from the stakeholder's real-time confirmation during the
      session — not guessed or carried over from the July stub.
- [ ] The existing `unverified-20260729` entry and the July fixture
      files under `tests/images/arducam-ov9782-usb-camera/` and
      `tests/images/hd-usb-camera/` are untouched.
- [ ] Every new manifest records camera 5's lighting condition
      (day/night) at capture time.
- [ ] The real daemon and all cameras are left in their normal,
      pre-session working state after the session ends (baseline
      controls restored; `aprilcam daemon start` brings everything back
      up normally) — confirmed by the operator before closing the
      Terminal session.
- [ ] All new fixture files, the updated `field-state.json`, and the
      per-camera manifests are committed to git.

## Implementation Plan

**Approach**: This is an operational ticket, not a code-authoring one.
The team-lead/operator runs
`python testdev/capture_matrix.py <camera> --field-state-id fs-20260823`
for each of cameras 2, 3, 5 per ticket 003's updated tool, in a Terminal
session, with the daemon stopped, following the ~15-minute procedure
named in the sprint's Test Strategy section. If the session surfaces a
problem with the capture tool itself (a bug, not an operator-confirmable
field-state question), that is a defect in ticket 003, reported back
before this ticket can complete — not silently worked around here.

**Files created/modified**:
- `tests/images/fs-20260823/{hd-usb-camera,arducam-ov9782-usb-camera,arducam-ov9281-usb-camera}/**`
  (new)
- `tests/images/field-state.json` (new `fs-20260823` entry)

**Testing plan**:
- No automated test in this ticket — verification is the operator's
  real-time confirmation plus the acceptance criteria above. Tickets
  005-007 are what write the automated tests that consume these
  fixtures.
- **Verification command**: N/A (operational ticket; no `pytest` run is
  this ticket's own gate — the resulting fixtures are exercised by
  ticket 005 onward).

**Documentation updates**:
- None beyond the manifest/field-state.json data itself.

## Risks Carried From sprint.md

- July's baseline captures recorded zero AprilTags. If this session
  repeats that outcome, tickets 005/007's AprilTag-specific recall and
  world-accuracy assertions gain a debugging detour before they can be
  written meaningfully — flag this immediately if observed, rather than
  silently writing tests against an empty AprilTag set.
- Camera-5 lighting variance must be recorded in the manifest (see
  Acceptance Criteria) since its exposure sweep spans day/night.
