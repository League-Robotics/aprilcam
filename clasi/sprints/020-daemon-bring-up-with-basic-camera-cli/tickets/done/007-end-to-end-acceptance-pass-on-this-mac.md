---
id: '007'
title: End-to-end acceptance pass on this Mac
status: done
use-cases:
- UC-01
- UC-13
- UC-14
- UC-17
- UC-25
depends-on:
- '006'
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

- [x] UC-01.S-2 — `aprilcam daemon start` with no config, cameras
      attached (baseline) or detached (verify empty-list success too)
      — both succeed. **Attached case verified live** (see Completion
      Notes): `daemon start` with the real config directories succeeded
      every time, enumerating 4 real cameras. **Detached case not
      re-verified live** — physically unplugging every camera from this
      Mac was outside what this ticket can do (see the note on UC-25.S-5
      below); the zero-camera path is instead covered by
      `tests/aprilcam/daemon/test_lifecycle.py::
      test_start_with_no_cameras_and_no_config_succeeds` and
      `tests/aprilcam/daemon/test_service.py::
      test_full_stack_start_with_zero_cameras_and_zero_config_succeeds`,
      both of which start the real assembled stack with a faked-empty
      identity source and assert success. Flagged for the stakeholder to
      confirm by hand once, by unplugging every camera and running
      `aprilcam daemon start`.
- [x] UC-01.S-1 — with both cameras attached, `aprilcam camera list`
      shows both with enumeration numbers; `aprilcam daemon stop` then
      `aprilcam daemon start` again; `camera list` shows the **same**
      enumeration numbers. Verified live across 3 consecutive
      stop/start cycles plus the initial start — identical `[1]
      brio-501 [2] hd-usb-camera [3] arducam-ov9782-usb-camera [4]
      usb2-video` every time (4 cameras attached to this Mac, not just
      the two named in the ticket header — see Completion Notes).
- [x] UC-01.S-3 / UC-13 `AlreadyRunning` — start a second daemon while
      the first runs; confirm it refuses and names the existing
      instance; confirm the first daemon and its camera list are
      undisturbed. Verified live: second `daemon start` exited 1 with
      `error: a daemon is already running: pid 66396`; the first
      daemon's `status`/`camera list` were byte-identical before and
      after the refused attempt.
- [x] UC-13.S-3 — full `status` (not running) → `start` → `status`
      (running) → `stop` round trip. Verified live, full round trip
      plus 3 extra restart cycles (see Completion Notes).
- [x] UC-13.S-4 — `daemon start -d <any-non-local-host>` refuses,
      pointing at SSH. Verified live: `aprilcam daemon start -d
      pi-field.local` exited 1 with `error: 'aprilcam daemon start'
      cannot target a remote host ('pi-field.local'); process lifecycle
      is local-only by design -- SSH to that host and run 'aprilcam
      daemon start' there instead (UC-13.S-4).`; `daemon status`
      immediately after confirmed nothing was started.
- [x] UC-14.S-1 — `camera list` output correctly names both real
      cameras with correct enumeration numbers and identity. Verified
      live: `arducam-ov9782-usb-camera` and `hd-usb-camera` both
      present, `usable`, correctly named, alongside 2 other cameras
      this Mac also has attached (`brio-501`, `usb2-video`) — all 4
      correctly enumerated and named, none omitted.
- [x] UC-25.S-1 / UC-25.S-2 — `camera probe` against both real cameras,
      independently reporting each one's exposed settings. Verified
      live: `camera probe` reported all 4 real cameras independently,
      each `present`/`usable`; re-run while a capture was in flight
      correctly reported the two busy cameras as `skipped` ("in use by
      a live subscriber") rather than disturbing them (see UC-25.S-7
      below — this sprint's `probe`/`list` report presence and
      usability, not yet per-setting detail, which is a later sprint's
      scope per `daemon/service.py`'s module docstring).
- [x] UC-25.S-5 — unplug and replug one camera while the daemon runs;
      confirm `camera list` reflects the change without running
      `probe`. **Physical unplug/replug not performed** — no way to
      unplug hardware from this environment (per this ticket's brief).
      Verified as much as possible programmatically instead: this
      mechanism was already covered headless by ticket 003's
      `tests/aprilcam/camera/test_discovery.py::
      test_watch_detects_hotplug_and_unplug_without_probe`, which
      simulates a backend appearing/disappearing via a faked identity
      source and asserts `CameraDiscovery.known_present()` tracks it
      via the passive watch loop alone (no `probe()` call), and that
      `DaemonService.list_cameras()` reports a registry record as
      `present=False` rather than removing it when not currently
      enumerated (`test_probe_reports_registry_record_absent_when_not_
      currently_enumerated`). **Flagged for the stakeholder**: physically
      unplug and replug one of the two fixture cameras while `aprilcam
      daemon start` is running and confirm `aprilcam camera list`
      reflects both transitions within a few seconds, with no `probe`
      run in between.
- [x] UC-25.S-7 — run `camera probe` while a `camera image` capture is
      in flight (or immediately before/after in quick succession);
      confirm neither disrupts the other. Verified live: ran `camera
      probe` and `camera image arducam-ov9782-usb-camera -o
      frame_concurrent.png` concurrently (backgrounded, `wait`ed on
      both). Both exited 0; the probe reported `hd-usb-camera` and
      `arducam-ov9782-usb-camera` under `skipped: ... in use by a live
      subscriber` while still reporting all 4 cameras present/usable;
      the capture produced a valid 87168-byte PNG, 1280×800.
- [x] UC-17.S-1 — `camera image <camera> -o frame.png --encoding png`
      produces a valid, lossless PNG (spot-check: file size/hash
      differs from a `--encoding jpeg` capture of the same moment, and
      the PNG opens correctly). Verified live and independently (not
      just exit-code): `file(1)` and Pillow both confirm `frame.png` is
      a genuine PNG, 1280×800, RGB, matching `arducam-ov9782-usb-camera`
      (OV9782's native resolution). A same-camera `--encoding jpeg`
      capture immediately after was 24489 bytes vs. the PNG's 86621
      bytes, different SHA-256, confirming the PNG is not merely a
      renamed JPEG. Both frames render as real (very low-light but
      genuinely noisy, non-flat) camera data — see Completion Notes.
- [x] UC-17.S-2 / UC-17.S-3 — stdout raw-bytes and base64 output modes
      both work as documented. Verified live: `camera image
      hd-usb-camera > frame.jpg` produced a 63232-byte file that
      `file(1)` and Pillow confirm is a genuine 1920×1080 JPEG (HD
      camera's native resolution). `camera image hd-usb-camera --base64
      | head -c 80` produced `/9j/4AAQSkZJRgABAQAAAQABAAD/...` — valid
      base64 whose decoded prefix is a real JPEG/JFIF header.
- [x] UC-17.S-4 — raw-bytes mode to a real attached terminal refuses
      as documented (run interactively, not just under a mocked
      `isatty`). Verified live under a genuine pty (`script -q /dev/null
      aprilcam camera image ... `, not a mocked `isatty`): refused with
      exit 1 and the documented message. The same command with `--force`
      exited 0 and wrote the frame; the written bytes were confirmed
      byte-identical to a normal capture once 4 leading bytes of
      `script`(1)'s own pty-session preamble were stripped (see
      Completion Notes) — the CLI's own output was not corrupted.
- [x] Full test suite (`uv run pytest`) passes headless with no camera
      attached (skips `@pytest.mark.camera` tests cleanly, no failures,
      no errors) — confirms the no-hardware-where-possible standing
      rule actually holds for this sprint's contribution. `uv run
      pytest` (no `-m` filter) passes 215/215 on this Mac, where camera
      tests run for real rather than skip (cameras are attached here).
      The headless-skip path itself could not be exercised live for the
      same physical-hardware-access reason as above, so it was verified
      by code review instead: every `@pytest.mark.camera` test module
      (`test_camera.py`, `test_discovery.py`, `test_cli_integration.py`,
      `test_lifecycle_integration.py`) has its own runtime
      `pytest.skip(...)` guard keyed off real hardware enumeration
      (`resolve_all()` / `_real_camera_index()`), independent of the
      marker — so a plain `pytest` on a genuinely headless machine
      would skip them via that guard, not merely via `-m` deselection.
      `uv run pytest -m "not camera"` was additionally run here to
      confirm the marker-deselection path: 209/209 passed, 6 deselected.
- [x] Full test suite passes on this Mac with both cameras attached,
      including all `@pytest.mark.camera` tests from tickets 002–006.
      `uv run pytest` → 215 passed, 0 failed, 0 errors. `uv run pytest -m
      camera` → 6 passed, 209 deselected (all camera-marked tests from
      tickets 002–006 ran for real and passed).

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

## Completion Notes

**Hardware actually on this Mac.** Not just the two named fixture
cameras — 4 are attached: `arducam-ov9782-usb-camera`, `hd-usb-camera`,
plus `brio-501` and `usb2-video`. All scenarios below ran against all 4;
nothing was omitted or hidden.

### Carried issue 1 — start-readiness race (real defect, fixed)

Confirmed real, not hypothetical, and fixed at the daemon, not the
client. Root cause: `daemon/lifecycle.py`'s `DaemonLifecycle.start()`
built and started the gRPC control-plane listener *before* running the
initial camera enumeration (`daemon/__main__.py`'s
`enumerate_then_watch`). The listener's thread pool begins dispatching
RPCs the instant it starts, concurrently with the enumeration still
running on the caller's thread — so `client/lifecycle.py`'s `start()`,
which defines "ready" purely as "the control-plane socket accepts a
connection," could return before the registry was populated, and an
immediate `camera list`/`daemon status` could observe an empty or
partial result.

**Fix**: reordered `DaemonLifecycle.start()` (`src/aprilcam/daemon/
lifecycle.py`) to run the initial enumeration (`watch()`) *before*
building/starting the listener, so the control-plane socket does not
exist at all until the daemon genuinely knows its cameras. This makes
socket-reachable and enumeration-complete the same event, fixing the
race for every reader of that signal — the client's own poll, a bare
`grpc.channel_ready_future`, any future health-check RPC — not just one
call site. `client/lifecycle.py` needed no code change; its module
docstring now explains why the fix belongs in the daemon (documented
alongside `daemon/lifecycle.py`'s own updated module/method docstrings).

**Regression test**: `tests/aprilcam/daemon/test_lifecycle.py::
test_start_calls_watch_before_building_the_control_plane_listener`
(headless, fake listener) asserts the `watch` hook fires strictly
before `build_listener`, on both a first `start()` and a subsequent
restart. The existing `test_start_with_no_cameras_and_no_config_succeeds`
was updated to the new event order (`["watch", "build_listener"]`).

**Live confirmation**: 3 consecutive `daemon stop` + `daemon start`
cycles each took ~3.2–3.4s (the real cost of enumerating 4 physical
cameras) and each immediately reported the correct camera count with
zero variance — versus a single early, anomalous `daemon start` that
returned in 0.38s reporting 0 cameras. That anomaly is explained below
(pre-existing colliding daemon on this machine), not by a residual bug
in the fix — a start that fast cannot have done real enumeration, so it
was answered by something other than this ticket's newly-started
daemon.

**Knock-on test fix.** Fixing the race broke
`tests/aprilcam/client/test_lifecycle.py::
test_start_stop_status_round_trip_against_a_real_daemon_process`
(`@pytest.mark.integration`, spawns a real subprocess against real
`Directories()`), which asserted `status.cameras == ()` unconditionally
with the comment "no fixture cameras required." That assertion was only
ever true *by accident of the race* on a machine with cameras attached:
before the fix, the RPC could win the race against enumeration and see
an empty registry even with hardware present; after the fix it
deterministically sees the real attached set, correctly failing the old
assertion on this Mac (4 cameras). Rewrote the assertion to check shape
(`isinstance(status.cameras, tuple)`, every entry named) rather than
assuming hardware absence — the test's actual purpose is the
start/status/stop process round trip, not a camera census.

### Carried issue 2 — `tests/testdev/detect_test.py` collection error

Did not reproduce as a hard failure in this environment specifically
because this project's `.venv` already has `opencv-contrib-python`
installed (the `daemon` extra) — `detect_test.py`'s top-level `import
cv2` succeeds here, so it silently collects as an empty module (0 test
items, since it has no `test_*` functions). But the defect is real: any
install without the `daemon` extra (this project's explicitly
opencv-free base install) would still hit `ModuleNotFoundError: No
module named 'cv2'` at collection time, before a single real test runs,
because `detect_test.py` incidentally matches pytest's default
`*_test.py` discovery glob despite being a standalone dev script (see
its own docstring: `python testdev/detect_test.py --camera 2`), not a
pytest suite.

**Fix**: added `tests/testdev/conftest.py` with `collect_ignore =
["detect_test.py"]`, scoped to exactly that one file — not a
project-wide `python_files` change, which would risk silently hiding a
genuine test file elsewhere in the tree that happens to end in
`_test.py`. Verified the mechanism directly (not just "no error today,"
which is not conclusive given cv2 is present here): built an isolated
repro directory with a `*_test.py` that imports a guaranteed-missing
module, confirmed it fails collection without the `conftest.py` and
collects cleanly (0 items, no error) with it.

### Carried issue 3 — `@pytest.mark.integration`/`camera` marker registration

Already done — both markers were already registered in `pyproject.toml`
(`[tool.pytest.ini_options].markers`) by the time this ticket started,
presumably as part of tickets 005/006's own work. No action needed;
verified with `uv run pytest tests/ -W error::pytest.PytestUnknownMarkWarning`
(passes clean, i.e. no unregistered-marker warning) and confirmed both
marker sets skip cleanly (`-m "not camera and not integration"` → 207
passed, 8 deselected; `-m camera` → 6 passed, 209 deselected; `-m
integration` → 2 passed, 213 deselected).

### Defect / finding: unrelated v1 daemon collides on the shared
control-plane socket path

While running the live acceptance sequence, the very first `daemon
start` + `camera list` produced `error: daemon at 'localhost' did not
answer: Method not found!` and a suspiciously instant (0.38s) "0
camera(s)" start. Investigation (`lsof`, `ps`) found an unrelated,
long-running (9.5h uptime, ~105% CPU) v1 daemon process from a sibling
checkout — `/Volumes/Proj/proj/RobotProjects/AprilTags/.venv/bin/
python3 -m aprilcam.daemon` (pid 66993) — with live file descriptors on
this exact machine's conventional control-plane socket
(`/var/folders/.../aprilcam-501/control.sock`), plus its own
`aprilcamd.pid`/`aprilcamd.lock` files under the same `aprilcam-501`
runtime directory. Both v1 and this v2 project resolve the same
XDG-style machine-wide singleton paths by design (`docs/design/
configuration.md`: sockets are not configurable) — but v1 apparently
does not use v2's `flock`-based `daemon.lock` mechanism (it has its own
`aprilcamd.lock`/`aprilcamd.pid`), so the two are not mutually exclusive
and can transiently collide on the shared socket path. This is a
pre-existing environmental hazard, **not introduced by this ticket** and
not a defect in this sprint's code — but it is a real hazard for
*running this v2 CLI on any machine that also runs a v1 daemon*, worth
the stakeholder's attention outside this ticket's scope. It did not
recur after the first collision (all subsequent live commands in this
session were clean and consistent); pid 66993 was left completely
untouched throughout (confirmed still running, unaffected, at the end
of this session). Two unambiguously-this-project stray daemon processes
(pid 43558, 61870 at various points — leaked from earlier ticket 006
manual testing, never `stop`ped) were found and cleaned up as part of
getting to a clean baseline; this is routine hygiene, not a code change.

### Minor finding (non-blocking): `BrokenPipeError` traceback on `--base64 | head`

`aprilcam camera image hd-usb-camera --base64 | head -c 80` printed the
correct 80 bytes of valid base64 (decodes to a real JPEG/JFIF header),
but also printed an unhandled `BrokenPipeError` traceback to stderr once
`head` closed the pipe early. Common with any Python CLI piping to
`head`/`less` without a `SIGPIPE`/`BrokenPipeError` guard; not part of
this ticket's acceptance criteria and not fixed here (no ticket owns
it), but recorded as a small UX polish opportunity for a future ticket.

### What the stakeholder should verify by hand

1. **UC-25.S-5, physical leg**: with `aprilcam daemon start` running,
   physically unplug then replug one fixture camera; confirm `aprilcam
   camera list` reflects both transitions within a few seconds with no
   `probe` run in between.
2. **UC-01.S-2, physical leg**: unplug every camera, run `aprilcam
   daemon start`, confirm it still succeeds and reports an empty camera
   list.
3. **v1/v2 daemon collision** (see above): if this machine (or any
   machine) may run both an older `aprilcam` checkout's daemon and this
   v2 daemon at the same time, be aware they can transiently fight over
   the same conventional control-plane socket path. Not this ticket's
   defect to fix, but worth deciding how to handle (e.g. a version- or
   project-scoped socket path) in a future sprint if this is a real
   deployment concern rather than purely a shared-dev-machine artifact.
