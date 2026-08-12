---
id: '006'
title: Integration pass, full-suite/mypy sweep, and hardware-blocked documentation
status: open
use-cases: [SUC-001, SUC-002, SUC-003, SUC-004, SUC-005, SUC-006, SUC-007]
depends-on: ['002', '005']
github-issue: ''
issue: tag-generation-and-daemon-discovery.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Integration pass, full-suite/mypy sweep, and hardware-blocked documentation

**Depends on**: 002, 005 (both feature halves — tag generation and
daemon discovery — must be `done` before this closing pass runs).
**Execute strictly serially.** Sprint 028 has no worktree opt-in
(`worktree: false` in `sprint.md`). Do not dispatch this ticket
concurrently with any other ticket in this sprint — it is the last
ticket in the sequence; every other ticket must be `done` first.

**No background-notification mechanism is available in this
environment.** Run every test synchronously, in-session, and read the
actual result before moving on — do not start a long-running
verification and move on expecting to be notified when it finishes.
Sprint 027's ticket 008 (`clasi/sprints/done/027-streams/tickets/done/
008-integration-acceptance-pass-and-hardware-blocked-documentation.md`)
is this ticket's direct template for both the completion-notes honesty
standard and the skip-cleanly pattern below — read it before starting.

**Serves**: all seven SUCs (SUC-001 through SUC-007) — this ticket does
not add feature behavior, it verifies that every acceptance criterion
across tickets 001-005 actually holds together, end to end, and
documents exactly what could not be verified this sprint.

## Description

Three things, all real work, none skippable:

1. **Full-suite and `uv run mypy` sweep** across everything tickets
   001-005 built — not a per-ticket re-check (each ticket already ran
   its own tests), but the one place the *whole* sprint's acceptance
   criteria are checked together: tag generation and daemon discovery
   changes coexisting cleanly (e.g. the `pyproject.toml` dependency
   move from ticket 001 does not break anything ticket 003's `zeroconf`
   usage needs, and vice versa), zero `typing.Protocol` anywhere
   touched by this sprint, mypy clean across `src/aprilcam`.
2. **Hardware-blocked documentation, honestly** — this sprint has
   exactly two acceptance criteria that need real hardware/environment
   resources this sprint cannot provide (`sprint.md`'s Hardware
   Constraints section, written during planning):
   - **Printing a generated tag and confirming detection on the real
     field** (issue verification bullet 1) — needs a real camera and a
     real printed tag.
   - **`daemon list` finding a genuine second physical machine** (issue
     verification bullet 3) — needs a real second host on the LAN (e.g.
     a Raspberry Pi), not two processes on one dev machine.
   Both must be **written, correctly marked, and skip cleanly** (or
   fail with a clear, non-misleading message) — never silently
   assumed-passing, matching sprint 027 ticket 008's own convention
   exactly. This project's `pytest.ini`/`pyproject.toml` markers are
   currently `integration` (real local daemon/hardware processes) and
   `camera` (real capture hardware attached) — neither literally means
   "a second physical host," but `sprint.md`'s own Hardware Constraints
   section calls both blocked criteria "`@pytest.mark.camera`-
   equivalent." Use `@pytest.mark.camera` for both (reusing this
   project's existing "blocked by unavailable physical resources"
   idiom rather than inventing a new marker mid-sprint, which is not in
   this sprint's Scope) — but say so explicitly in the test's own
   docstring/skip message for the second-machine case, so a future
   reader is not confused into thinking a webcam is what is missing.
3. **Cross-check `sprint.md` against what actually got built.** Tickets
   001-005 may have made small, reasonable implementation choices that
   diverge from `sprint.md`'s provisional suggestions (constant names,
   the exact DPI/size-warning threshold from ticket 001, the browse-
   result shape from ticket 004, etc.) — that is expected and fine; it
   must be **recorded**, not silently left stale (same rule sprint 027
   ticket 008 applied). Update `sprint.md`'s Hardware Constraints /
   Migration Concerns sections if reality diverged from the plan.

## Acceptance Criteria

- [ ] Full existing suite (baseline as of ticket 005, plus every test
      added by tickets 001-005) passes. Report the actual final count
      in this ticket's completion notes, not the baseline.
- [ ] `uv run mypy` clean across the whole sprint's changes
      (`src/aprilcam`, per this repo's `[tool.mypy] files` scope).
- [ ] Zero `typing.Protocol` anywhere touched by this sprint (grep
      confirms it — record the grep result in completion notes, per
      sprint 027 ticket 008's precedent).
- [ ] `@pytest.mark.camera` test(s) exist for **printed-tag detection
      on the real field** — generate a tag via `TagGenerator`, print it
      (manual step, documented in the test's own docstring/skip
      message since printing cannot be automated), and confirm
      detection via the existing detection pipeline. Skips cleanly
      (clear message naming what is needed to run it for real) on a
      machine with no usable camera/printed tag available, mirroring
      `test_detection_hardware.py`'s or `test_streaming_hardware.py`'s
      existing skip-cleanly precedent (see the 027-008 template).
- [ ] `@pytest.mark.camera` test(s) exist for **`daemon list` finding a
      genuine second physical machine** — attempts real mDNS discovery
      against whatever is actually on the LAN when the test runs, and
      **skips cleanly with an explicit, honest message** (naming that a
      genuine second host is required, distinguishing this from the
      two-local-daemon-alternate-port mechanism test that tickets
      004/005 already run for real and that does pass without special
      hardware) rather than either (a) silently passing because it
      happens to find nothing and treats that as "no daemons, which is
      valid," or (b) hard-failing in an environment that was never
      expected to have a second host. If a second real host genuinely
      is reachable in the environment this ticket executes in, let the
      test run for real and record the result honestly (mirroring
      027-008's own experience: its hardware constraint was overstated
      in planning and running the suite anyway found real, informative
      results — do the same here rather than assuming the worst case
      without checking).
- [ ] `sprint.md`'s Hardware Constraints section is updated to
      cross-reference this ticket's actual findings (which of the two
      blocked criteria genuinely could not run, and why, vs. anything
      that turned out to be checkable after all) rather than left as
      the pre-execution planning guess.
- [ ] `sprint.md`'s Architecture (module table, class/constant names)
      is cross-checked against what tickets 001-005 actually built; any
      divergence (naming, shape, chosen DPI/size threshold, browse-seam
      shape, etc.) is recorded in this ticket's completion notes, not
      silently left mismatched.
- [ ] A short, explicit "blocked pending [X]" note is added to this
      ticket's own completion notes (and cross-referenced from
      `sprint.md` if not already present there) naming exactly which
      criteria could not be verified this sprint and what running them
      later requires (a real printed tag + camera for the first; a
      genuine second physical host on the LAN for the second).
- [ ] Every full-suite integration scenario from tickets 001-005's own
      Test Strategy still passes together (not just individually per
      ticket) — in particular, both real integration tests (ticket
      004's two-local-daemon `find_all()` test, ticket 005's fan-out-
      verification test) still pass when run as part of the whole
      suite, not only in isolation.

## Implementation Plan

**Approach.** This ticket adds no new production code — tests and
documentation only, at the seam where tickets 001-005's work meets
(mirrors sprint 027 ticket 008's own approach exactly). Read
`sprint.md`'s Architecture, Test Strategy, and Hardware Constraints
sections again before writing anything here, since this is the one
ticket that checks the *whole* sprint's acceptance criteria together
rather than per-module.

**Files to create/modify:**
- New hardware test module(s) for the two blocked criteria — e.g.
  `tests/aprilcam/tags/test_taggen_hardware.py` (printed-tag
  detection) and `tests/aprilcam/client/test_discovery_hardware.py` or
  `tests/aprilcam/cli/test_daemon_cli_hardware.py` (genuine
  second-machine `daemon list`) — naming and placement is this ticket's
  call; follow this repo's existing `test_*_hardware.py` naming
  convention (`test_detection_hardware.py`,
  `test_streaming_hardware.py`, `test_calibration_hardware.py`,
  `test_mobile_tags_hardware.py`) rather than inventing a new one.
- `sprint.md` (this sprint's own doc) — Hardware Constraints/
  Architecture cross-check updates, as described above.

**Testing plan:**
- Run the full suite twice: once with `-m "not camera"` (the headless
  baseline everyone else's CI/dev loop uses) to confirm the sprint adds
  no regression there, and once with no marker filter (full suite,
  camera tests included) to see what the two new hardware tests
  actually do in this execution environment — honestly report both
  results, per 027-008's precedent of running the hardware suite even
  when planning assumed it could not do anything (which then turned
  out to be an overstatement worth correcting).
- `uv run mypy`, full clean run.

**Documentation updates:** `sprint.md`'s Hardware Constraints section
is the deliverable here, not a side effect — update it with this
ticket's actual findings.

## Testing

- **Existing tests to run**: the full suite, `uv run pytest`, run to
  completion and read in full — this is the ticket where "full suite
  passes" is the actual acceptance bar, not an aside. Also run
  `uv run pytest -m "not camera"` separately to confirm the headless
  baseline is unaffected.
- **New tests to write**: see Acceptance Criteria above — two
  `@pytest.mark.camera` hardware tests, written, correctly marked, and
  skipping cleanly (or running informatively for real, if the
  environment genuinely supports it).
- **Verification command**: `uv run pytest && uv run mypy` — run
  synchronously, in-session, and read the real output. Do not report
  this ticket done without having actually seen a passing run.
