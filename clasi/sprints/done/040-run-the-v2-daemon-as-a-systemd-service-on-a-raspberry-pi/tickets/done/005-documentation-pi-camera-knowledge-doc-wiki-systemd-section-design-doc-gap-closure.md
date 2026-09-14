---
id: '005'
title: 'Documentation: Pi camera knowledge doc, wiki systemd section, design-doc gap
  closure'
status: done
use-cases: []
depends-on:
- '004'
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

- [x] `docs/knowledge/raspberry-pi-camera-setup.md` exists, is v2-accurate
      (no v1 file paths, no `eric`/`jtl` usernames presented as current),
      and includes a ROS-coexistence subsection sourced from ticket 004's
      recorded state changes.
- [x] `docs/knowledge/v4l2loopback-vanishes-after-kernel-upgrade.md`
      ported (or explicitly noted as not-yet-relevant, with reasoning) —
      do not silently skip it.
- [x] `docs/wiki/daemon.md`'s systemd section reflects the real `deploy/`
      assets, not the placeholder two-line example.
- [x] `docs/design/design.md` §9 item 2 and `camera/DESIGN.md`'s matching
      Open Questions entries are marked resolved with a sprint-040
      pointer, following the existing "Resolved, sprint NNN" convention.
- [x] Both linked issues carry an explicit "already covered by v2" vs.
      "closed by this sprint" breakdown before being archived.
- [x] No source-code changes in this ticket (docs only); `scripts/
      test-gate.sh` passes (should be a no-op check, confirming docs-only
      changes didn't accidentally touch code). **One pre-existing
      environmental flake found, unrelated to this ticket — see
      Findings.**

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

## Findings

1. **`src/aprilcam/daemon/DESIGN.md` has no statement claiming the
   loopback backend is unverified — checked, nothing to close there.**
   The ticket brief named this file alongside `camera/DESIGN.md` as
   needing the "unverified" language updated; a full grep of
   `daemon/DESIGN.md` for `libcamera`/`loopback`/`unverified`/`Pi`/
   `Raspberry`/`vidar` found zero hits. The "V4l2LoopbackBackend is
   unverified"/"no libcamera capture backend" statements live entirely
   in `camera/DESIGN.md` (now resolved, see above) and
   `docs/design/design.md` §9 (now resolved, see above) — `daemon/
   DESIGN.md` never independently duplicated them. No change made to
   this file; noted here rather than silently skipped, per this
   project's convention for a named-but-not-actually-present item.
2. **`src/aprilcam/client/DESIGN.md`** — already updated by ticket
   040-007 (its IPv4-preferred client-resolution entry is in place and
   consistent with this ticket's own findings); no further change
   needed.
3. **`src/aprilcam/camera/backends/v4l2_loopback.py`'s module docstring**
   updated (in scope per this ticket's brief) to replace its "not
   exercised against real frames this ticket" caveat with the sprint 040
   ticket 003 live-verification result — a docstring/comment change only,
   no behavior change.
4. **`scripts/test-gate.sh` — one pre-existing, environment-caused
   failure, unrelated to this ticket's docs-only changes.**
   `tests/aprilcam/client/test_discovery.py::
   test_find_all_with_the_real_browse_default_returns_promptly_when_nothing_answers`
   deliberately performs a real, bounded `zeroconf.ServiceBrowser` scan
   and asserts it finds nothing; it failed because a **real AprilCam
   daemon (`192.168.1.144:5280`, i.e. `vidar.local`) is currently
   mDNS-advertising on this LAN**, consistent with the team-lead's
   concurrent live switch-over of vidar to run `aprilcamd` per this
   ticket's own sprint.md Open Question 1 resolution (see above) — the
   exact same class of environment contamination
   `docs/knowledge/close-sprint-test-gate-hardware-flakes.md` already
   documents for camera-hardware tests, here manifesting for a
   network-discovery test instead. Re-ran the single test in isolation
   and confirmed the identical failure with the identical real address,
   ruling out test-order interference. This ticket touched no `.py` file
   outside a docstring comment (item 3 above) and no code in
   `client/discovery.py` or `tests/aprilcam/client/`; the scoped test
   run for the one file actually touched
   (`tests/aprilcam/camera/test_backends.py`, 29 passed) is clean, and
   the full gate's own count (`2209 passed, 48 skipped, 107 deselected,
   1 failed`) is otherwise identical in shape to ticket 004's last clean
   run (`2210 passed, 48 skipped, 107 deselected`) modulo this one
   network-dependent test. Reported honestly rather than silently
   waived; not fixed here (out of this docs-only ticket's scope, and not
   a defect in any file this ticket owns).
