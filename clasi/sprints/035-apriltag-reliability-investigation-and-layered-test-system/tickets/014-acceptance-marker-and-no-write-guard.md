---
id: '014'
title: Acceptance marker and no-write guard
status: open
use-cases: [UC-29]
depends-on: []
github-issue: ''
issue: aprilcam-v2-layered-test-system.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Acceptance marker and no-write guard

## Description

Starts ticket-group 4 (L2): registers the new `acceptance` pytest marker
(layered on top of `camera`+`integration`, never replacing them, so the
existing gate's `-m "not camera and not integration"` exclusion
expression needs zero edits), and builds `tests/acceptance/`'s
foundational conftest: a requirement that isolation environment
variables are NOT set (this suite refuses to run under
`APRILCAM_CONFIG_DIR`/`APRILCAM_STATE_DIR` overrides — it exists
specifically to test the real thing), and the sha256 no-write guard that
hashes every file under the real `config_dir`/`state_dir` before the
suite runs and again after, hard-failing on any diff. Also adds a
terminal-summary hook rendering a human-readable PASS/FAIL/SKIP report.

This is the mechanism that makes the sprint's "sacred `camera_position`"
invariant provable, not assumed — every later L2 ticket depends on this
guard actually working.

## Acceptance Criteria

- [ ] `pyproject.toml`'s `markers` list gains one entry: `"acceptance:
      tests against the real daemon and real field state (deselect with
      -m 'not acceptance')"`. The existing `camera`/`integration`
      entries and the gate's exclusion expression are unedited.
- [ ] `tests/acceptance/conftest.py` refuses to run (hard error, not
      skip) if `APRILCAM_CONFIG_DIR` or `APRILCAM_STATE_DIR` is set in
      the environment — this suite requires the real roots.
- [ ] The no-write guard hashes (sha256) every file under the real
      `config_dir`/`state_dir` at session start and again at session end
      (or per-test, whichever the conftest design lands on — document
      the choice), and raises a clear, per-file-named error on any
      mismatch.
- [ ] The no-write guard is itself unit-tested independent of any real
      daemon: construct a fake tree, touch a file inside it mid-"session",
      confirm the guard raises naming that exact file.
- [ ] A terminal-summary pytest hook renders a human-readable
      PASS/FAIL/SKIP report at the end of an acceptance run.
- [ ] Every test in `tests/acceptance/` (even a placeholder in this
      ticket) carries all three of `acceptance`+`camera`+`integration`
      markers — verified by a small conftest-level check or a dedicated
      marker-audit test, not left to convention alone.

## Implementation Plan

**Approach**: Build the guard as a session-scoped autouse fixture in
`tests/acceptance/conftest.py`, hashing via `hashlib.sha256` over a
sorted file walk (deterministic ordering for reproducible diagnostics).
Keep the guard itself dependency-free (no daemon/camera imports) so its
own unit test can run with zero hardware and zero real system directory
risk (operate on a fake tree in a tmp_path, never the real one, even in
the guard's own test).

**Files to create/modify**:
- `pyproject.toml` — new `acceptance` marker.
- `tests/acceptance/__init__.py` (new package)
- `tests/acceptance/conftest.py` — real-roots requirement, no-write
  guard, terminal-summary hook.
- `tests/acceptance/test_no_write_guard.py` — the guard's own unit test
  (fake tree, never the real system).

**Testing plan**:
- The guard's own unit test needs no real hardware and no real system
  directories.
- **Verification command**: `uv run pytest tests/acceptance/test_no_write_guard.py -v`

**Documentation updates**:
- None yet — ticket 017 covers the runner script and README/docs
  section for the whole L2 layer.
