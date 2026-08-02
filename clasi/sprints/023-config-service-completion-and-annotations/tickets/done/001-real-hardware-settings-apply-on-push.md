---
id: '001'
title: Real hardware settings apply on push
status: done
use-cases:
- SUC-002
depends-on: []
github-issue: ''
issue: config-service-completion-and-annotations.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Real hardware settings apply on push

## Description

Wire the camera-config push path to actually change hardware, and settle
whether OpenCV's `cap.set()` is trustworthy for this hardware or whether
a `uvc-util` subprocess adapter is needed.

Sprint 022 shipped `DaemonService.set_camera_config` as store-only —
`config.settings` round-trips but is never applied (`daemon/service.py`'s
own docstring says so explicitly: "no `Camera.apply_config` call happens
here"). The ordering/hard-error contract this needs already exists and
is unit-tested in `camera/camera.py`'s `Camera.apply_config` (auto-mode
gate keys pushed first via `_GATE_KEYS`, whole-push rollback on an
unsupported key) — it has simply never been wired to a caller, and never
validated against real hardware.

**Stated approach — validate first, do not assume.** Archived v1
evidence (`archive/src/aprilcam/camera/DESIGN.md` §4,
`archive/camera_setup.sh`) is that v1 never actually trusted a generic
capture-library property setter for exposure/gain/white-balance on this
exact hardware — it always shelled out to the native `uvc-util` CLI,
because a *different* generic tool (`uvcc`) was found broken on modern
macOS ("UVCAssistant claims the USB interface and every libusb control
transfer stalls"). That evidence is against `uvcc`, not against OpenCV's
`cap.set()` specifically, so do not assume `cap.set()` is broken — but do
not assume it works either. Determine this for real, on camera 3, before
building anything further:

1. Push a manual low-exposure config to camera 3 through the (not-yet-
   wired) `apply_config` path.
2. Capture a frame before and after.
3. If the after-frame is visibly, measurably darker: `cap.set()` works.
   Wire it through as-is — no new backend needed.
4. If it is not: build `camera/backends/uvc_util.py`, an adapter with the
   same `get_property`/`set_property`/`settable_names` shape
   `OpenCVBackend` already exposes (see
   `camera/backends/opencv_backend.py`), backing `exposure`/`gain`/
   `white_balance_temperature`/`auto_exposure`/`auto_white_balance` via
   `uvc-util -N <device_name> -S <ctrl>` / `-s <ctrl>=<value>` subprocess
   calls (binary at `~/.local/bin/uvc-util` on this dev machine; search
   `PATH` generally, do not hardcode that path). Mirror
   `archive/camera_setup.sh`'s proven sequence exactly: `auto-exposure-
   mode=1` (manual) before `exposure-time-abs`; `auto-white-balance-
   temp=false` before a WB temperature value. OV9782 control ranges
   (from the same script, queried against this exact hardware):
   `exposure-time-abs` 1..5000 in 100µs ticks (default 157), `gain`
   0..100 (default 0), `auto-exposure-mode` 1=manual / 8=aperture-
   priority. Keep `resolution` (frame width/height) on the OpenCV path
   regardless — it is not a UVC control.

**Invoke `uvc-util` safely, if built.** `subprocess.run([...],
shell=False)` — an argument list, never a shell string. A config-
document value reaching a subprocess command line is exactly the shape
of thing that must not become an injection surface, even on a trusted
local network.

Either way, `Camera.apply_config`'s existing ordering (`_GATE_KEYS`) and
whole-push rollback-on-failure contract must not be weakened, and
`DaemonService.set_camera_config` must call it when the camera is
currently held open (UC-03.S-2's "applies... immediately, not at next
open").

## Acceptance Criteria

- [x] `DaemonService.set_camera_config` calls `Camera.apply_config` when
      the target camera is held open; a config for a camera not
      currently open is still stored (unchanged behavior) with nothing
      to apply yet.
- [x] Real-hardware determination made and recorded (code comment /
      ticket close notes): does `cap.set()` work for exposure/gain/WB on
      camera 3, yes or no.
- [x] If no: `camera/backends/uvc_util.py` exists, implements
      `get_property`/`set_property`/`settable_names` matching
      `OpenCVBackend`'s shape, invokes `uvc-util` via
      `subprocess.run([...], shell=False)`, and is wired into `Camera`
      for the controls it owns while resolution stays on
      `OpenCVBackend`.
- [x] UC-03.S-2 verified live on camera 3: pushing a manual
      low-exposure config produces a visibly darker frame captured
      immediately after, versus one captured before.
- [x] UC-03.S-3 verified live: pushing a config with an unsupported key
      or an out-of-range value to a real camera fails the **whole**
      push, names the offending key, and leaves the camera's hardware
      state unchanged (re-fetching its config after the failed push
      shows the pre-push values).
- [x] UC-03.S-4 verified live: camera 3
      (`arducam-ov9782-usb-camera`) and `hd-usb-camera` configured
      independently in the same test session — pushing to one does not
      change the other.
- [x] Existing ordering discipline (`_GATE_KEYS`: `auto_exposure`/
      `auto_white_balance` pushed before their dependent values) and
      rollback-on-failure (partially-applied settings reverted to their
      pre-push values on an `UnsupportedSetting`) remain intact,
      covered by the existing headless mocked-backend tests, extended
      if the uvc_util backend is added.
- [x] `uv run mypy` clean (`tests/test_typing.py` enforces it); baseline
      629 passed / 1 skipped still green; new tests additive; zero
      `typing.Protocol` in any new interface.

## Close Notes

**`cap.set()` finding: does not work — confirmed live, decisively.**
Validated first, per the ticket's stated approach, before building
anything. `cv2.VideoCapture.set()` (`OpenCVBackend.set_property`)
returned `False` for **every one** of `auto_exposure`/`exposure`/
`gain`/`auto_white_balance`/`white_balance_temperature` when pushed
live to camera 3 (`arducam-ov9782-usb-camera`, an Arducam OV9782 USB
Camera) through the newly-wired `DaemonService.set_camera_config` ->
`Camera.apply_config` path — not the ambiguous "reports success but
nothing changes" failure mode, OpenCV itself refuses outright on this
platform's AVFoundation capture session. This is a genuinely new
finding beyond archived v1 evidence: v1's record
(`archive/camera_setup.sh`) is explicit that `uvcc` (a different
generic macOS UVC tool) does not work, but says nothing about OpenCV's
`cap.set()` specifically — this ticket determined that for real rather
than assuming either way.

`camera/backends/uvc_util.py` (`UvcUtilBackend`) was built as a result
and is the mechanism real hardware now uses (wired unconditionally on
macOS via `DaemonService._default_control_backend_factory`, not a
fallback tried only if OpenCV fails first — the ticket's own Migration
Concerns rule out silently falling back to a control path already
proven not to work). Verified live end to end through the real
`DaemonService`: pushing `exposure=2000` then `exposure=6` to camera 3
changed a captured frame's mean grayscale brightness from **5.6** to
**52.2** and back to **5.6** — confirmed independently via `uvc-util`
control readback at every step. UC-03.S-3 (unsupported key, and
separately an out-of-range value) and UC-03.S-4 (camera 3 +
`hd-usb-camera` configured independently in one session) were also
verified live, not just headlessly.

**A genuine hardware-state bug was found and fixed during live
verification, not merely simulated.** The first live out-of-range test
(`exposure=99999`, real max 5000) revealed that `uvc-util` can report
success (exit 0) while silently *clamping* to the nearest valid bound
instead of rejecting — leaving the hardware at `5000`, neither the
requested nor the original value, even though the push correctly
reported failure at the config-document level. Fixed by having
`UvcUtilBackend.set_property` snapshot the pre-call value and, on a
readback mismatch, write it straight back before returning `False` —
verified by re-running the identical live experiment afterward and
confirming the hardware now stays exactly at its pre-push value. Full
writeup: `docs/knowledge/2026-08-01-uvc-util-camera-control-macos.md`
(includes the settle-delay gotcha found while writing the
`@pytest.mark.camera` brightness test too).

**Camera 3 restored.** Every real-hardware experiment run during this
ticket (manual CLI verification and the automated `@pytest.mark.camera`
tests, which each carry their own `finally`-block restoration) ended
with camera 3's hardware settings back at its original baseline —
`auto-exposure-mode=1`, `exposure-time-abs=6`, `gain=2`,
`auto-white-balance-temp=false`, `white-balance-temp=4600` — verified
via direct `uvc-util` readback, and its `main-playfield` link was
re-pushed before handing the daemon back. `hd-usb-camera` (the
independence test's second camera) was restored to its factory-default
`auto-exposure-mode=8`.

**For ticket 002 (self-describing config template):** `Camera`'s
settings surface now genuinely reflects real hardware on macOS via
`UvcUtilBackend.settable_names()` (exactly the five controls: `auto_exposure`,
`exposure`, `gain`, `auto_white_balance`, `white_balance_temperature`) —
`effective_config()` was deliberately left unchanged this ticket (still
only introspects `device_name`/`frame_width`/`frame_height`), since the
"every settable key, blank when unset" template is ticket 002's own
scope, sequenced after this one precisely so it has a real per-camera
answer to report instead of a placeholder. `frame_width`/`frame_height`
as a settable key should route through the *primary* `OpenCVBackend`
(`Camera._backend`), never through `control_backend` — `_settings_backend()`
in `camera/camera.py` currently returns one or the other exclusively,
so ticket 002 will need to special-case resolution rather than adding
it to `UvcUtilBackend`'s control set (it is not a UVC control at all).

**For ticket 003 (boot-time config load):** `DaemonService.set_camera_config`
applies to hardware only when the camera is already held open in
`self._cameras` — a config loaded at boot for a camera not yet opened
is stored but not applied (matching this ticket's AC1, unchanged). The
sprint's ticket breakdown already assigns "apply-on-open for a
boot-loaded camera config" to ticket 003 (`_camera_for`'s open path),
not this one — that wiring still needs to be built; nothing here does
it. `_default_control_backend_factory(device_name)` is the function to
reuse/call from wherever ticket 003 ends up needing a `Camera`
constructed with the real control backend.

## Implementation Plan

**Approach:** see Description — validate `cap.set()` on camera 3 first;
build the `uvc_util` backend only if that validation fails.

**Files to modify:**
- `src/aprilcam/daemon/service.py` — `set_camera_config`: after
  storing, if `config.name` is a currently held-open camera
  (`self._cameras`), call `cam.apply_config(config)`.
- `src/aprilcam/camera/camera.py` — only touch if the real-hardware
  finding requires adjusting how `apply_config` selects a backend
  property path.

**Files to create (conditional on the validate-first finding):**
- `src/aprilcam/camera/backends/uvc_util.py` — the subprocess-backed
  control adapter, only if `cap.set()` is found not to work.

**Archive-reuse pointers:**
- `archive/camera_setup.sh` — the exact working `uvc-util` invocation
  sequence and OV9782 control ranges, if the uvc_util backend is
  needed.
- `archive/src/aprilcam/camera/DESIGN.md` §4 — "the daemon's open path
  drains 5 warm-up frames before applying UVC controls — AVFoundation
  resets controls applied before its capture session settles" and
  "re-applied on every reopen" — worth checking whether this
  warm-up-frames caveat still applies if the uvc_util path is built
  (v2's `Camera.open()` currently has no such drain step).

## Testing

- **Existing tests to run**: `tests/aprilcam/camera/test_camera.py`,
  `tests/aprilcam/daemon/test_service.py`, full `uv run pytest` for the
  629/1 baseline.
- **New tests to write**: headless — `DaemonService.set_camera_config`
  calling `apply_config` on a held-open camera (mock `Camera`); if the
  uvc_util backend is built, mock `subprocess.run` and assert command
  construction (argument list, no shell) plus error-to-
  `UnsupportedSetting` mapping. `@pytest.mark.camera` — the four UC-03
  scenarios above against camera 3 (+ `hd-usb-camera` for
  independence); skip cleanly without hardware.
- **Verification command**: `uv run pytest` (full suite); `uv run
  pytest -m camera` on a machine with the real cameras attached; `uv
  run mypy`.
