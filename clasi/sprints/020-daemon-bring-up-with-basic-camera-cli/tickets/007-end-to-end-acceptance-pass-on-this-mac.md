---
id: '007'
title: End-to-end acceptance pass on this Mac
status: open
use-cases: [UC-01, UC-13, UC-14, UC-17, UC-25]
depends-on: ['006']
github-issue: ''
issue: daemon-bring-up-with-basic-camera-cli.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# End-to-end acceptance pass on this Mac

## Description

Depends on ticket 006 (the full stack — daemon, client, CLI — must
exist to run end-to-end). No new production code is expected from this
ticket; it is the sprint's closing verification pass, run against real
hardware (`arducam-ov9782-usb-camera`, `hd-usb-camera`) on this Mac,
confirming every UC scenario cited across sprint.md's SUC-001 through
SUC-004 actually holds when driven from the shell — not just through
mocked/headless unit tests from tickets 001–006. If a scenario fails
here, the fix belongs in the ticket that owns the failing subsystem
(reopen it), not in this ticket.

This ticket also produces the sprint's closing evidence: a session
transcript or log capturing each command run and its output, referenced
from the sprint's closure review.

**Reuse from archive**: none — this is a verification ticket, not an
implementation one.

## Acceptance Criteria

Run each of the following live on this Mac and confirm the cited
scenario's observable outcome, in order:

- [ ] UC-01.S-2 — `aprilcam daemon start` with no config, cameras
      attached (baseline) or detached (verify empty-list success too)
      — both succeed.
- [ ] UC-01.S-1 — with both cameras attached, `aprilcam camera list`
      shows both with enumeration numbers; `aprilcam daemon stop` then
      `aprilcam daemon start` again; `camera list` shows the **same**
      enumeration numbers.
- [ ] UC-01.S-3 / UC-13 `AlreadyRunning` — start a second daemon while
      the first runs; confirm it refuses and names the existing
      instance; confirm the first daemon and its camera list are
      undisturbed.
- [ ] UC-13.S-3 — full `status` (not running) → `start` → `status`
      (running) → `stop` round trip.
- [ ] UC-13.S-4 — `daemon start -d <any-non-local-host>` refuses,
      pointing at SSH.
- [ ] UC-14.S-1 — `camera list` output correctly names both real
      cameras with correct enumeration numbers and identity.
- [ ] UC-25.S-1 / UC-25.S-2 — `camera probe` against both real cameras,
      independently reporting each one's exposed settings.
- [ ] UC-25.S-5 — unplug and replug one camera while the daemon runs;
      confirm `camera list` reflects the change without running
      `probe`.
- [ ] UC-25.S-7 — run `camera probe` while a `camera image` capture is
      in flight (or immediately before/after in quick succession);
      confirm neither disrupts the other.
- [ ] UC-17.S-1 — `camera image <camera> -o frame.png --encoding png`
      produces a valid, lossless PNG (spot-check: file size/hash
      differs from a `--encoding jpeg` capture of the same moment, and
      the PNG opens correctly).
- [ ] UC-17.S-2 / UC-17.S-3 — stdout raw-bytes and base64 output modes
      both work as documented.
- [ ] UC-17.S-4 — raw-bytes mode to a real attached terminal refuses
      as documented (run interactively, not just under a mocked
      `isatty`).
- [ ] Full test suite (`uv run pytest`) passes headless with no camera
      attached (skips `@pytest.mark.camera` tests cleanly, no failures,
      no errors) — confirms the no-hardware-where-possible standing
      rule actually holds for this sprint's contribution.
- [ ] Full test suite passes on this Mac with both cameras attached,
      including all `@pytest.mark.camera` tests from tickets 002–006.

## Testing

- **Existing tests to run**: the complete `tests/aprilcam/` suite from
  tickets 001–006, both headless and `-m camera`, as the last two
  acceptance criteria above.
- **New tests to write**: none required, though a lightweight
  `tests/aprilcam/test_acceptance_020.py` scripted walkthrough of the
  above (marked `@pytest.mark.camera`, skipped in CI) is encouraged so
  this pass is repeatable rather than purely manual.
- **Verification command**: `uv run pytest` (headless pass) then
  `uv run pytest -m camera` (hardware pass) on this Mac.
