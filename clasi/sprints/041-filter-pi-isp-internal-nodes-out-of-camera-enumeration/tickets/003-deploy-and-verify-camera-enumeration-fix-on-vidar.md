---
id: '003'
title: Deploy and verify camera enumeration fix on vidar
status: open
use-cases: [SUC-001, SUC-002]
depends-on: ['001', '002']
github-issue: ''
issue: linux-enumeration-lists-pi-isp-nodes-as-cameras-with-duplicate-rows.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Deploy and verify camera enumeration fix on vidar

## Description

Live, system-level verification that tickets 001 and 002 actually fix
the symptoms observed on `vidar.local` (Pi 5, Ubuntu 24.04 aarch64, two
imx296 CSI cameras, AprilCam v2 installed as a systemd service by
sprint 040). Offline unit tests (tickets 001/002) prove the logic is
correct against fakes; this ticket proves it against the real hardware
and the real, already-polluted registry that motivated the issue.

**This ticket is the only one in this sprint permitted to touch
vidar.** Everything else is local build/packaging work on the Mac.

### Permitted state changes on vidar

- Install a newly-built `aprilcam` wheel via `pipx` as the `aprilcam`
  system user (per `deploy/README.md`'s runbook) — replacing the
  currently-installed `0.20260914.1` build.
- Restart the `aprilcamd` systemd service (and its bridge units, if the
  runbook calls for it) to pick up the new install.
- Read-only inspection: `aprilcam camera probe -d vidar.local`,
  `dmesg`, `journalctl` for `aprilcamd`, `systemctl status` for the
  camera-related units, reading (not editing) `registry.json` under
  `aprilcam`'s state dir to confirm the purge.

### Explicitly not permitted on vidar

- No manual edits to `registry.json` or any other state file — the
  purge must happen automatically via the restarted daemon (ticket
  002's fix), or this ticket fails and 002 needs revisiting, not a
  hand fix.
- No changes to the four ROS units, no re-enabling anything ROS-related
  — the process notes require vidar's *final* state to still be
  AprilCam owning the cameras (ROS units disabled, `aprilcamd` + bridges
  enabled and active), exactly as sprint 040 left it. This ticket must
  leave that state unchanged (or restore it before finishing, if a
  restart transiently altered it).
- No `sudo systemctl restart libcamerasrc`/direct camera-tool
  invocations (e.g. `cam -c1`) beyond what's needed to read status —
  the process notes warn a SIGSEGV against the wedged `imx296` camera
  once froze vidar's sshd. Do not exercise `imx296` beyond what `camera
  probe` itself does.
- No kernel/DKMS/v4l2loopback changes — that is a separate, already-
  resolved concern (`docs/knowledge/v4l2loopback-vanishes-after-kernel-
  upgrade.md`); this ticket assumes the loopbacks are already present
  and working, matching vidar's current state per the sprint's
  Evidence section.

## Acceptance Criteria

- [ ] A wheel built from this sprint's merged code (tickets 001+002) is
      installed on vidar via `pipx` as the `aprilcam` user, per
      `deploy/README.md`.
- [ ] `aprilcamd` (and any dependent bridge units) is restarted and
      reports active/running afterward.
- [ ] `aprilcam camera probe -d vidar.local` lists exactly two cameras,
      `imx296` and `imx296-2`, each exactly once — no `rp1-cfe-*`,
      `pispbe-*`, or other internal-node rows, no duplicate rows.
- [ ] `imx296` (i2c@88000, known hardware-faulty — "Camera frontend has
      timed out") appears present but `usable=False`; `imx296-2`
      appears present and `usable=True`.
- [ ] `imx296` and `imx296-2` retain registry numbers 1 and 2
      respectively (unchanged from before this deploy) — confirmed by
      reading (not editing) the registry state.
- [ ] The previously-registered junk rows (vidar's `[3]..[12]`,
      `rp1-cfe-*`) are gone from the registry after the restart, with
      no manual file edit.
- [ ] A frame can still be captured from `imx296-2` (e.g.
      `aprilcam camera image -d vidar.local imx296-2` or equivalent, per
      whatever the CLI exposes — confirm the exact command from
      `aprilcam camera --help` rather than assuming one).
- [ ] `dmesg` on vidar, sampled after the restart and a short
      observation window, no longer accumulates new `node link is not
      enabled` lines for either CSI controller (some pre-existing lines
      from before the restart are expected and fine — only new
      accumulation after the fix is deployed matters).
- [ ] vidar's final state matches sprint 040's baseline: all four ROS
      units disabled, `aprilcamd` and bridge units enabled and active.
- [ ] No manual registry edits, no destructive commands against
      `imx296`, no ROS re-enablement occurred during this ticket.

## Implementation Plan

**Approach**:

1. On the Mac, merge/rebase onto the sprint branch with tickets 001 and
   002 complete, then build the wheel per `deploy/README.md`.
2. Follow `deploy/README.md`'s runbook to `scp`/transfer and `pipx
   install --force` (or the runbook's equivalent) the wheel on vidar as
   the `aprilcam` user, via `ssh ros@vidar.local` with passwordless
   sudo as needed for the service restart step only.
3. Restart `aprilcamd` (`sudo systemctl restart aprilcamd` or whatever
   the runbook names) and any dependent bridge units.
4. Run the read-only verification commands listed in Acceptance
   Criteria, capturing output for the ticket's own record (paste
   relevant excerpts into this ticket file or the closing commit
   message — implementer's judgment).
5. If any acceptance criterion fails, do **not** patch it by hand on
   vidar — reopen ticket 001 or 002 as appropriate (per
   `docs/knowledge`'s "fix-up after close gate failure" convention:
   reopen the owning ticket, dispatch a fix, redeploy) and re-run this
   ticket's verification from step 1.

**Files to modify**: None in the repository (deployment/verification
only). If the verification surfaces a knowledge-worthy gotcha (e.g. a
runbook step that needed adjusting for this fix), add or extend a
`docs/knowledge/` entry — that is source-adjacent documentation, not
gated by `.claude/rules/source-code.md`.

**Testing plan**: This ticket *is* the test — live system verification
per the Acceptance Criteria above. No new offline automated tests are
added here (those belong to tickets 001/002).

**Documentation updates**: Update `docs/knowledge/` only if the live
deploy surfaces a non-obvious gotcha worth recording for next time
(e.g. anything about the pipx reinstall step, service restart ordering,
or dmesg observation timing that wasn't already documented).

## Testing

- **Existing tests to run**: None (no code changes in this ticket).
  The offline gate (`scripts/test-gate.sh`) already ran for tickets
  001/002; this ticket does not re-run it.
- **New tests to write**: None — this is live/system verification, not
  unit testing.
- **Verification command**: The manual sequence in the Implementation
  Plan above (`ssh ros@vidar.local`, `aprilcam camera probe -d
  vidar.local`, `dmesg`, `systemctl status aprilcamd`).
