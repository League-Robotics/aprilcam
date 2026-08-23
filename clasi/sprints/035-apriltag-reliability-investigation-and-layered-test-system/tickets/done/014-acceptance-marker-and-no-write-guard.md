---
id: '014'
title: Acceptance marker and no-write guard
status: done
use-cases:
- UC-29
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

- [x] `pyproject.toml`'s `markers` list gains one entry: `"acceptance:
      tests against the real daemon and real field state (deselect with
      -m 'not acceptance')"`. The existing `camera`/`integration`
      entries and the gate's exclusion expression are unedited.
- [x] `tests/acceptance/conftest.py` refuses to run (hard error, not
      skip) if `APRILCAM_CONFIG_DIR` or `APRILCAM_STATE_DIR` is set in
      the environment — this suite requires the real roots.
- [x] The no-write guard hashes (sha256) every file under the real
      `config_dir`/`state_dir` at session start and again at session end
      (or per-test, whichever the conftest design lands on — document
      the choice), and raises a clear, per-file-named error on any
      mismatch.
- [x] The no-write guard is itself unit-tested independent of any real
      daemon: construct a fake tree, touch a file inside it mid-"session",
      confirm the guard raises naming that exact file.
- [x] A terminal-summary pytest hook renders a human-readable
      PASS/FAIL/SKIP report at the end of an acceptance run.
- [x] Every test in `tests/acceptance/` (even a placeholder in this
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

## Implementation Notes

**Design landed as: per-test comparison against a single session
baseline.** `real_directories` and `_no_write_baseline` are
session-scoped fixtures (the sha256 snapshot of every file under both
real roots is taken exactly once, before the first test runs); the
autouse `_no_write_guard` fixture is function-scoped and re-hashes both
roots after *every* test, comparing against that same original
baseline (never a rolling one). This gets the acceptance criterion's
"or per-test" branch: a violation is caught and named as close as
possible to the test that caused it, while the baseline itself is only
computed once (comparing against the untouched original, not a moving
target, is what lets the failure message point at the exact file that
changed the first time the guard notices it).

**The live-override vs on-disk-file distinction (asked for explicitly
in the dispatch).** `daemon.service.DaemonService.push_config` (the MCP
`set_camera_config` path) replaces the daemon's in-memory
`_camera_configs` entry for a camera immediately and does **not** write
`config_dir/cameras/<slug>/config.json` — only `Camera.reset()` re-reads
the on-disk file and discards a live override
(`src/aprilcam/daemon/service.py` around `_camera_reset`,
`src/aprilcam/camera/camera.py` sprint.md Decision 3). Consequence: the
sha256 no-write guard is honest about **files** and silent about
**runtime daemon state**. If some other process pushes a live
`camera_position` override to the daemon while this suite runs, the
guard still reports green — the on-disk file genuinely did not change —
even though the running daemon would answer geometry queries with
different numbers than what is on disk. This is not a guard bug: it is
scoped to what it is named for. Two failure modes this is meant to
head off:
  - *False confidence*: a green no-write guard must never be read as
    "nothing about the daemon's behavior changed" — only as "the files
    on disk are byte-identical to the session's start." The read-only/
    reversible acceptance inventory (ticket 015+) is what keeps this
    honest in practice, by never calling anything that pushes a live
    override in the first place.
  - *False alarm*: conversely, a live override pushed by some other
    concurrent process is not something this guard can or should flag —
    it is real-time daemon state, not a file write, and sha256-hashing
    file contents (sprint.md Decision 3's explicit choice, over
    mtime/size) will correctly not see it.
  This is documented at length in `tests/acceptance/conftest.py`'s
  module docstring (the "What the no-write guard does *not* prove"
  section) so it stays next to the code it describes, not only here.

**Guard unit tests** (`tests/acceptance/test_no_write_guard.py`, 7
tests) cover: deterministic content-keyed hashing, a missing root
hashing to `{}`, an unchanged tree passing silently, and
`NoWriteViolation` naming the exact file on a modified/added/removed
file — plus one test confirming a change *outside* the given root is
correctly ignored (`hash_tree` is scoped to its argument, not the whole
sandbox). All operate on a fake tree under `tmp_path`; none touch the
real `~/.config/aprilcam` or `~/.local/share/aprilcam`.

**Verification performed** (foreground, scoped to this ticket):
- `uv run pytest tests/acceptance/test_no_write_guard.py -v` — 7
  passed, 0 failed. The terminal-summary hook rendered:
  ```
  =========================== acceptance layer summary ===========================
  result:  PASS
  passed:  7
  failed:  0
  skipped: 0
  ```
- Gate-exclusion check (must hold with **zero edits** to the existing
  expression): `uv run pytest -m "not camera and not integration" -q
  tests/acceptance/` → `7 deselected in 0.01s` — confirms the new
  `acceptance` marker, layered on top of `camera`+`integration` per
  sprint.md Decision 4, keeps the whole directory out of the fast gate
  automatically.
- Real-roots requirement, exercised both ways: `APRILCAM_CONFIG_DIR=
  /tmp/fake-config uv run pytest tests/acceptance/ -q` and the same
  with `APRILCAM_STATE_DIR` → both hard-fail at `pytest_configure` with
  exit code `4` (`EXIT_USAGEERROR`, not a skip) and a message naming the
  offending env var, before any test collects.
- Marker-audit hook, exercised with a throwaway unmarked probe test
  file dropped into `tests/acceptance/` and removed immediately after:
  collection aborted with exit code `4` and a message naming the exact
  node id and the three missing markers; `git status` confirms no
  probe file was left behind.
- Real `camera_position` spot-check before/after this entire session:
  `~/.config/aprilcam/cameras/arducam-ov9782-usb-camera/config.json`
  unchanged — `{'height_cm': 128.22990496814816, 'nadir_x_cm':
  3.0570142285481654, 'nadir_y_cm': -2.798700632324644}`, sha256
  `15dd06a2ea5aec1352c5bb8ff018207b3c5828ac31ca2d78f5a9f325518520be`.
- No real daemon was started, restarted, or stopped for any of the
  above — every command above touches only `tests/acceptance/`'s own
  fixtures and fake trees, or reads (never writes) the real config/state
  roots via `hash_tree`.
