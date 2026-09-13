---
id: '005'
title: 'Documentation: Pi camera knowledge doc, wiki systemd section, design-doc gap
  closure'
status: open
use-cases: []
depends-on: ['004']
github-issue: ''
issue:
- plan-daemon-runs-on-a-remote-raspberry-pi-every-client-discovers-it-and-connects-over-tcp.md
- live-tag-detection-and-systemd-persistence-on-a-pi-with-a-calibrated-playfield.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Documentation: Pi camera knowledge doc, wiki systemd section, design-doc gap closure

## Description

Close out the sprint's documentation debt now that tickets 001-004 have
produced real findings to document — writing this before live
verification would mean guessing at what actually happened.

1. **Port and update the Pi camera setup knowledge doc.** CLAUDE.md
   already links `docs/knowledge/raspberry-pi-camera-setup.md` as an
   example entry, but it currently only exists under
   `archive/docs/knowledge/` (v1). Port it to `docs/knowledge/` per this
   project's knowledge-base convention (`CLAUDE.md`'s "Knowledge base"
   section), updating every v1-specific detail to v2 (module paths, the
   `aprilcam` service user instead of `eric`/`jtl`, `deploy/` asset names
   instead of ad hoc scripts) and adding what this sprint learned:
   whatever ticket 003 found wrong with the capture path, whatever ticket
   004 found during live install/reboot, and — new, not in the v1
   doc — the **ROS-coexistence procedure**: exactly which services to
   stop/disable to free the CSI cameras for AprilCam, and how to restore
   them, drawn directly from ticket 004's recorded before/after state.
   Also port `v4l2loopback-vanishes-after-kernel-upgrade.md` if this
   sprint's findings make it still relevant to v2 (likely yes — the
   underlying DKMS/kernel-header failure mode is unrelated to which
   daemon version reads the loopback device).
2. **Update `docs/wiki/daemon.md`'s systemd section** — replace the
   "the repo ships no unit files yet" / two-line illustrative unit with a
   pointer to the real `deploy/` assets and a short summary of what they
   do, matching the finished, verified state (not the aspirational one).
3. **Close the known-gap entries**: `docs/design/design.md` §9 item 2
   ("No libcamera capture backend exists yet") and
   `src/aprilcam/camera/DESIGN.md`'s matching Open Questions entries
   (the "No libcamera capture backend exists yet" and "`V4l2LoopbackBackend`
   is unverified against real frames" items) — mark each resolved, per
   the project's existing convention for closed gaps (design.md §9 item 1's
   "Resolved, sprint 036" phrasing), naming this sprint and what was
   actually verified/fixed. Do not delete the history — extend it, the
   same way item 1 in design.md §9 was extended rather than removed.
4. Move both linked issues (`plan-daemon-runs-on-a-remote-raspberry-pi-...md`,
   `live-tag-detection-and-systemd-persistence-...md`) to resolved via the
   proper CLASI mechanism when this ticket (the last one referencing both)
   is moved to done — each issue should read, by the time it's archived,
   an explicit note of which of its original workstreams/deferred items
   v2 already covered before this sprint vs. what this sprint closed.

## Acceptance Criteria

- [ ] `docs/knowledge/raspberry-pi-camera-setup.md` exists, is v2-accurate
      (no v1 file paths, no `eric`/`jtl` usernames presented as current),
      and includes a ROS-coexistence subsection sourced from ticket 004's
      recorded state changes.
- [ ] `docs/knowledge/v4l2loopback-vanishes-after-kernel-upgrade.md`
      ported (or explicitly noted as not-yet-relevant, with reasoning) —
      do not silently skip it.
- [ ] `docs/wiki/daemon.md`'s systemd section reflects the real `deploy/`
      assets, not the placeholder two-line example.
- [ ] `docs/design/design.md` §9 item 2 and `camera/DESIGN.md`'s matching
      Open Questions entries are marked resolved with a sprint-040
      pointer, following the existing "Resolved, sprint NNN" convention.
- [ ] Both linked issues carry an explicit "already covered by v2" vs.
      "closed by this sprint" breakdown before being archived.
- [ ] No source-code changes in this ticket (docs only); `scripts/
      test-gate.sh` passes (should be a no-op check, confirming docs-only
      changes didn't accidentally touch code).

## Implementation Plan

**Approach**: Pull concretely from tickets 001-004's recorded findings —
this ticket should read as a synthesis of what actually happened, not a
restatement of the plan. Where a v1 knowledge-doc claim (e.g. exact
`max_buffers=16` rationale, the stagger timing) was reconfirmed unchanged
by this sprint's live work, keep it and cite the reconfirmation; where
something changed (service user, file layout, module paths), rewrite it.

**Files to create**: `docs/knowledge/raspberry-pi-camera-setup.md`,
`docs/knowledge/v4l2loopback-vanishes-after-kernel-upgrade.md` (or a
documented decision not to port it).

**Files to modify**: `docs/wiki/daemon.md`, `docs/design/design.md`,
`src/aprilcam/camera/DESIGN.md`.

**Testing plan**: `scripts/test-gate.sh` (docs-only change, expect a clean
pass with no diffs in behavior).

**Documentation updates**: this ticket *is* the documentation update —
no further downstream doc work expected.
