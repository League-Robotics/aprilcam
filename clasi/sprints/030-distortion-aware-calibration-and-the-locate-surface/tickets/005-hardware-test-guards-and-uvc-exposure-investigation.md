---
id: '005'
title: Hardware test guards and UVC exposure investigation
status: done
use-cases:
- SUC-005
depends-on: []
github-issue: ''
issue: hardware-tests-fail-instead-of-skipping-and-a-suspect-uvc-exposure-push.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Hardware test guards and UVC exposure investigation

## Description

A full-suite run gave 2 failed, 1385 passed, 4 skipped. Neither failure
is a regression in merged code — both are `@pytest.mark.camera` tests
failing hard on an under-guarded environment precondition, training
people to expect two red hardware tests, which is how a real third
failure would get missed. Two independent parts:

**Part A** — `test_camera_geometry_hardware.py`'s locate-reproduction
test guards "camera attached" and "reference tags present" but not "a
daemon is reachable," so it fails with `no local daemon answered`
instead of skipping. Add a third guard, same shape as the other two.

**Part B** — `test_service.py`'s two-real-camera exposure-push test
fails with `UnsupportedSetting: 'exposure'` behind a `uvc-util -s
exposure-time-abs=300 exited 13`. v2's UVC gate mechanism
(`_GATE_KEYS`, pushed first in `apply_config`) is present and correctly
ordered, so the cause is one of: the test's config doesn't actually set
`auto_exposure`; `auto_exposure` is pushed but its value doesn't put
*this* camera in manual mode; `300` is outside this camera's supported
range; or the second camera genuinely lacks the control. **Investigate
which before treating this as a skip-guard fix** — if it's a real
ordering/range bug it affects `camera config` pushes against real
hardware generally, not just this test.

## Acceptance Criteria

- [x] `test_camera_geometry_hardware.py::test_locate_reports_missing_references_or_reproduces_the_documented_fixture` gains a third guard (skip cleanly when no local daemon answers), matching the shape of `_skip_if_arducam_not_attached`.
- [x] The module docstring records the decision *not* to start an isolated `_short_tmp_dir` daemon for this specific test, unlike the module's other test — because this test's entire purpose is validating against the real production daemon's real, currently-held calibration/`camera_position` state, which an isolated daemon would not have.
- [x] Full suite green on a machine with cameras attached and no daemon running. (Scoped to this ticket's own modules, per the team-lead's explicit instruction that the literal full 1385-test suite is close-sprint's once-per-sprint gate, not a per-ticket one — see completion notes for exactly what ran.)
- [x] Full suite green (`uv run pytest -m "not camera"`) on a machine with no cameras attached — confirmed unaffected by this ticket's changes. (Cameras are physically attached in this environment, so verified via targeted headless runs across every module this ticket touched, not a literal no-camera machine — see completion notes.)
- [x] The UVC exposure-time-abs rejection in `test_set_camera_config_two_real_cameras_configured_independently` is investigated against real hardware (both cameras) to a stated conclusion identifying which of the four candidate causes is real: test-fixture gap, gate-value semantics, out-of-range value, or control genuinely absent — verified with actual evidence (e.g. `uvc-util -S` readback of `auto_exposure`'s value after the gate push; the camera's advertised `exposure-time-abs` range).
- [x] Whichever cause is confirmed is fixed at its source: a real ordering/semantics/range bug is fixed in `camera/camera.py`/`camera/backends/uvc_util.py`; a test-fixture gap is fixed in the test; a genuinely-absent control is reflected by correcting the test's premise about which settings are portable across these two camera models (not worked around by loosening an assertion). (None of the four applied — see completion notes for the fifth, evidence-backed conclusion and why no ordering/gating/range/test change was warranted.)
- [x] `UnsupportedSetting`'s message is improved to name the real cause category (still in auto mode / value out of range / control not supported) instead of the current generic "unsupported or rejected setting" — regardless of which cause was confirmed, since the current diagnostic is poor either way (issue's own Verification bullet).
- [x] The investigation's conclusion — which of the four candidates was real, and the evidence for it — is stated in this ticket's own completion notes, not just "fixed it."

## Testing

- **Existing tests to run**: `uv run pytest -m "not camera" -q` (confirm zero headless impact from either part); `uv run pytest tests/aprilcam/daemon/test_camera_geometry_hardware.py tests/aprilcam/daemon/test_service.py -v` on a machine with both cameras attached and no daemon running.
- **New tests to write**: the daemon-reachability guard itself (Part A) is the test change; Part B's fix is verified by the existing `test_set_camera_config_two_real_cameras_configured_independently` passing for real, plus (if a real backend bug was fixed) a new headless unit test pinning the corrected ordering/range-check behaviour in `camera/camera.py`/`uvc_util.py` so the fix doesn't require real hardware to regress-test in CI.
- **Verification command**: `uv run pytest -q` (full suite, on a machine with cameras attached and no daemon running) and `uv run pytest -m "not camera" -q` (on any machine)

## Completion Notes

### Part A — daemon-reachability guard

Added `_skip_if_no_local_daemon_reachable()` to
`tests/aprilcam/daemon/test_camera_geometry_hardware.py`, same shape as
`_skip_if_arducam_not_attached()`: an in-process, sub-second probe
(`client.discovery.probe_socket_reachable` against
`Directories().socket_dir / SOCKET_FILENAME`), not a subprocess round
trip. Called immediately after the existing camera-attached guard in
`test_locate_reports_missing_references_or_reproduces_the_documented_fixture`.
The module docstring gained a new section recording the deliberate
decision *not* to give this test its own isolated `_short_tmp_dir`
daemon (unlike the module's other test): its entire purpose is
reproducing the documented fixture against the *real* production
daemon's real, currently-held `camera_position`/calibration state, and
an isolated daemon on a fresh temp `socket_dir` would have none of
that state — self-containment here would trade a real check for a
permanently vacuous one.

Verified live, three separate times, with the default local control
socket genuinely unoccupied (`aprilcam daemon stop`; confirmed via
`aprilcam daemon status` → `stopped` and `lsof`/`ps` showing nothing
bound to `.../aprilcam-501/control.sock`): the test skips cleanly every
time, naming the missing daemon, instead of the pre-fix hard failure
(`no local daemon answered`, `assert 1 == 0`). With a real daemon
reachable, the guard correctly does *not* skip and the test proceeds to
its original two behaviours (real reproduction vs. clean "references
not staged" skip) unchanged.

### Part B — UVC exposure investigation

**Conclusion: none of the four candidate causes named in the issue is
real.** All four are falsified by direct real-hardware evidence
(`arducam-ov9782-usb-camera` and `hd-usb-camera`, both physically
attached):

1. **Test-fixture gap** (`auto_exposure` never set) — **false**. The
   test's own settings dict always includes `auto_exposure: 1` in the
   same push as `exposure`.
2. **Gate-value semantics** (`auto_exposure=1` doesn't achieve manual
   mode on this camera) — **false**. `uvc-util -N "HD USB CAMERA" -S
   auto-exposure-mode` reliably reads back `1` after every successful
   push, confirmed across 8+ real attempts via both a raw `uvc-util`
   CLI sequence and the full `Camera.apply_config`/
   `DaemonService.set_camera_config` code path.
3. **Out-of-range value** (`300` outside `hd-usb-camera`'s
   `exposure-time-abs` range) — **false**. `uvc-util -S
   exposure-time-abs` on `hd-usb-camera` reports `minimum: 1, maximum:
   10000` — `300` is well within range.
4. **Control genuinely absent** on the second camera — **false**.
   `exposure-time-abs`/`auto-exposure-mode` are demonstrably present
   and writable on `hd-usb-camera`, confirmed repeatedly by both direct
   `uvc-util` CLI writes and the real `Camera.apply_config` path
   succeeding.

**The real, evidence-backed conclusion**: the failure is a genuine but
**intermittent, non-deterministic hardware/driver-level write
rejection** — not a logic defect in this codebase's control ordering,
gating, or range validation. Reproduced twice independently during this
investigation (once on `exposure`, once — in a separate run — on
`auto_exposure` itself), both times via a harness identical in code
path to the failing test (a fresh `DaemonService` opening both real
cameras and pushing the exact same settings dicts), and both times an
immediate retry of the identical push succeeded. Across roughly 10 real
attempts at the exact push sequence this test performs, 2 failed and 8
succeeded, with no code, ordering, or value difference between
attempts. Both failures occurred shortly after other camera
capture-session activity on this shared machine (once immediately after
stopping an external daemon that had `arducam-ov9782-usb-camera` open;
once as the third of three back-to-back real-hardware tests each
opening/closing both cameras' AVFoundation sessions in quick
succession) — consistent with a transient USB/AVFoundation-level timing
race, not a reproducible ordering/range/gating bug.

Per the ticket's own instruction ("if it's a real bug, fix it; if the
camera genuinely lacks the control, correct the test's premise"),
neither applied, so `Camera.apply_config`'s ordering/gating logic and
the test's premise (the settings *are* portable across both camera
models) are both unchanged. What the ticket separately and
unconditionally requires — a better `UnsupportedSetting` diagnostic,
regardless of which cause was confirmed — is implemented:

- `errors.UnsupportedSetting` gained an optional `reason` (backward
  compatible: `.key` unchanged, every existing single-arg call site
  keeps working, message unchanged when `reason` is `None`).
- `camera.backends.ControlBackend` gained an optional
  `describe_set_failure(name, value) -> Optional[str]` diagnostic hook
  (defaults to `None` — unlike this class's other three methods, this
  one is a best-effort add-on, not part of the core get/set contract,
  so it does not `raise NotImplementedError`). Resolved via
  `getattr(backend, "describe_set_failure", None)`, the same
  duck-typed pattern `Camera.apply_config` already uses for
  `get_property`/`set_property` so `OpenCVBackend` (which does not
  derive from `ControlBackend`) is handled identically to a real
  subclass.
- `camera.camera.Camera._describe_set_failure` prefers the backend's
  own live-hardware answer; when the backend has nothing to say (e.g. a
  bare `FakeBackend`/`OpenCVBackend`), it falls back to a new
  config-shape-only check (`_GATED_BY`): whether the failing key is
  gated by another key (`exposure`→`auto_exposure`,
  `white_balance_temperature`→`auto_white_balance`) that this same push
  never included — the one cause a backend with no hardware readback
  can still be right about, and exactly candidate 1 from the issue.
- `camera.backends.uvc_util.UvcUtilBackend.describe_set_failure`
  checks, against live hardware, in order: (1) is the value outside the
  control's `uvc-util -S`-advertised range; (2) is the associated gate
  control's live current value not the known manual value
  (`auto-exposure-mode=1`, `auto-white-balance-temp=false` — sourced
  from `archive/camera_setup.sh`'s proven sequence and this project's
  own real-hardware convention, `docs/knowledge/2026-08-01-uvc-util-
  camera-control-macos.md`); (3) neither applies — states that
  explicitly (control recognized, value in range, gate reads back as
  manual, hardware still rejected the write, likely transient) rather
  than silently falling back to the old generic message. This exact
  message fired, correctly, in both real reproductions of the actual
  failure during this investigation.

New headless (no hardware needed) tests pin this behaviour for CI
regression: 5 in `tests/aprilcam/camera/test_uvc_util.py`
(`describe_set_failure`'s range check, gate check, honest
fallback-to-transient, and unknown-control/no-gate short-circuits, all
with `subprocess.run` mocked per this module's existing convention) and
3 in `tests/aprilcam/camera/test_camera.py` (`.reason` populated on the
unknown-key path; the `_GATED_BY` config-shape fallback when the
backend has no diagnostic; a backend's own `describe_set_failure`
taking priority over that fallback).

### Camera-marked failure audit (per the assignment's explicit ask)

A full `-m camera` sweep this session (2026-08-20, cameras attached, no
daemon running at sweep start) gave **19 failed, 23 passed, 7 skipped**
(1428 non-camera tests deselected) — larger than the 2 failures named
in the source issue and larger than ticket 004's own prior count.
Breakdown:

- **17 of 19** (`test_calibration_hardware.py` ×8,
  `test_detection_hardware.py` ×6, `test_lifecycle_integration.py`
  ×1, `test_mobile_tags_hardware.py` ×1, `mcp/test_integration.py`
  ×1) are consistent with **one already-known, already-filed
  environmental precondition**: the physical `arducam-ov9782-usb-camera`
  currently cannot see enough of the playfield's ArUco corner markers
  to calibrate (observed matching as few as 0–6 of the required 10
  ids). This is the same root cause ticket 004 already identified and
  attributed to "the physical arducam currently can't see the full
  playfield" — confirmed still present here, and by this raw count
  apparently *more* pervasive than when ticket 004 ran. I directly
  confirmed the FOV failure signature (full traceback, "missing ArUco
  id(s)") for `test_calibration_hardware.py`'s cluster (via this
  ticket's own `test_mount_z_...` run), `test_lifecycle_integration.py`,
  `test_mobile_tags_hardware.py`, and `mcp/test_integration.py`. I did
  **not** pull full tracebacks for `test_detection_hardware.py`'s 6
  failures (truncated out of the sweep's captured tail) — I infer they
  share this same cluster by module purpose and elimination (every
  `test_service.py` failure is accounted for separately below and is
  not in this list), but flag this specific inference as unconfirmed by
  direct traceback in my own investigation.
  **Left alone, deliberately**: this is squarely ticket 004's
  already-filed, already-diagnosed environmental issue, not a new
  defect, and not one of this ticket's two named items. Adding an
  FOV-precondition guard would touch five modules well outside this
  ticket's acceptance criteria; I recommend it be scoped as its own
  follow-up (physically re-aiming the camera, and/or a dedicated ticket
  for a "can the camera see the calibration target" skip guard across
  the calibration-dependent hardware-test modules) rather than folded
  into this one.
- **1 of 19** — this ticket's own
  `test_locate_reports_missing_references_or_reproduces_the_documented_fixture`,
  in the sweep specifically — is a **newly-discovered environmental
  hazard**, not a guard gap in this ticket's fix: this shared dev
  machine has at least one, and during this session as many as three
  sequentially, externally-managed `aprilcam` daemon processes (one
  from a sibling project's own vendored copy of this codebase, at an
  older version predating sprint 029's `camera locate` RPC; another
  explicitly bound to a distinct `/tmp/aprilcam-v1` socket path) that
  get started/restarted by something outside this session's control —
  most plausibly concurrent, live work by another actor on this same
  machine, plausibly on the very "configurable daemon instance identity
  for side-by-side v1/v2" issue visible (untracked) in this repo at
  session start. When one of those external, version-mismatched
  daemons happened to be bound to the *default* local control socket at
  the moment this test ran mid-sweep, the new guard correctly
  determined "yes, a daemon is reachable" (genuinely true) and let the
  test proceed — which then failed for a real, different reason
  (`Method not found!`, an RPC the external daemon's older build does
  not implement) rather than the `no local daemon answered` failure
  this ticket's guard targets. Confirmed independently, 3 separate
  times, that with the default socket genuinely unoccupied the guard
  skips cleanly every time (see Part A above) — this ticket does not
  attempt to also guard against "a daemon is reachable but is a
  different/incompatible build," since that is squarely the
  already-tracked side-by-side-identity issue's territory, not one of
  this ticket's two named items.
- **1 of 19** — this ticket's own `test_mount_z_...` — is the FOV
  cluster (confirmed via full traceback).
- **`test_service.py`: 0 failures** in the sweep (6/6 camera-marked
  tests passed) — re-run standalone, `test_set_camera_config_two_real_
  cameras_configured_independently` passed 3/3 additional times,
  consistent with Part B's conclusion that the original failure was a
  rare, transient event (roughly 1 in 5 real attempts across this
  session's cumulative testing), not a standing defect.

### Verification actually run this session

- `uv run pytest tests/aprilcam/daemon/test_camera_geometry_hardware.py
  tests/aprilcam/daemon/test_service.py -q` — **234 passed, 1 skipped**
  (cameras attached, no daemon on the default socket). The skip is the
  locate test's new guard, correctly firing.
- `uv run pytest tests/aprilcam/daemon/test_service.py -m camera -q` —
  6/6 passed (three separate times for the two-real-cameras test
  specifically, to check determinism).
- `uv run pytest tests/aprilcam/camera/ tests/aprilcam/daemon/
  test_camera_geometry_hardware.py tests/aprilcam/mcp/test_errors.py -m
  "not camera" -q` — 166 passed (headless slice across every module
  this ticket touched).
- `uv run pytest tests/aprilcam/daemon/test_service.py -m "not camera"
  -q` — 227 passed (headless regression across the module Part B's
  fix lives closest to).
- Did **not** re-run the literal full suite (`uv run pytest -q`,
  ~1385+ tests) — per the team-lead's explicit instruction that this is
  close-sprint's once-per-sprint gate, not a per-ticket one. Did not
  literally test on a no-camera machine either (cameras are physically
  attached here) — the "`-m "not camera"` unaffected" requirement is
  instead verified by the targeted headless runs above, which exercise
  every module this ticket's changes touch.

### Files changed

- `tests/aprilcam/daemon/test_camera_geometry_hardware.py` — Part A's
  guard, call site, and module/test docstring updates.
- `src/aprilcam/errors.py` — `UnsupportedSetting.reason` (optional,
  additive).
- `src/aprilcam/camera/backends/__init__.py` —
  `ControlBackend.describe_set_failure` (optional diagnostic hook).
- `src/aprilcam/camera/camera.py` — `_GATED_BY` map,
  `Camera._describe_set_failure`, the three `UnsupportedSetting` raise
  sites in `apply_config` now pass a `reason`.
- `src/aprilcam/camera/backends/uvc_util.py` — `_GATE_FOR`/
  `_GATE_MANUAL_VALUE` maps, `_MINIMUM_RE`/`_MAXIMUM_RE`,
  `_show_control_range`, `UvcUtilBackend.describe_set_failure`.
- `tests/aprilcam/camera/test_uvc_util.py` — 5 new headless tests for
  `describe_set_failure`.
- `tests/aprilcam/camera/test_camera.py` — 3 new/extended headless
  tests for `.reason` propagation and backend-priority fallback.
