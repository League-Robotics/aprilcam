---
id: '002'
title: Dark-frame / zero-detection advisory signal
status: done
use-cases:
- SUC-002
depends-on: []
github-issue: ''
issue: mcp-playfield-registry-not-restored-after-daemon-restart.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Dark-frame / zero-detection advisory signal

## Description

The OV9782 tag camera runs a very short, fixed exposure
(`exposure-time-abs: 6`, `gain: 2`, set in
`data/aprilcam/cameras/arducam-ov9782-usb-camera/config.json` and applied by
`_apply_camera_settings()` in `src/aprilcam/daemon/camera_pipeline.py:52-97`)
tuned for a well-lit field. A small lighting dip blanks detection entirely —
the daemon continues capturing valid (non-`None`) frames, but they are too
dark for AprilTag/ArUco detection, so `get_tags` silently returns zero or
near-zero tags. Investigation during sprint planning confirmed there is
currently **no signal anywhere** in the daemon that would make this
condition observable: no brightness/histogram check, no zero-detection
counter. The only existing "health" proxy,
`consecutive_failures` (`camera_pipeline.py` ~line 805-806), only detects a
dead capture device (`cap.read()` returning falsy) — it does not notice a
technically-valid-but-too-dark frame.

This ticket adds a lightweight, advisory-only signal so the condition is
observable via `info.json` and logs, without attempting to fix the
underlying exposure fragility. Remediation strategy (auto-exposure,
adaptive gain, detection-side gamma/CLAHE compensation, or an
operator-facing alert beyond `info.json`/logs) is **explicitly out of
scope** for this ticket and this sprint — see `architecture-update.md` §2
and Open Question 4 for the reasoning: deciding the correct remediation
needs field data this sprint does not have, and this ticket's job is to
start producing that data via the diagnostic signal.

## Acceptance Criteria

- [x] `src/aprilcam/daemon/camera_pipeline.py`: the per-pipeline frame loop
      computes a cheap **mean-luminance sample** (downsample + `numpy.mean`
      — pure NumPy, already a base daemon dependency; no new CV dependency)
      once per N frames (configurable constant; not every frame, to keep
      cost negligible), alongside the detector's already-computed per-frame
      tag count (no new detection work — reuse the existing count).
- [x] A **sustained** condition — mean luminance below a configurable floor
      **and** zero tags detected — over a configurable window (e.g. last 30
      frames ≈ 1s at 30fps) sets a new boolean, `frame_dark: bool`.
- [x] `frame_dark` is written to `info.json` via the existing
      write-on-change pattern already used by
      `_maybe_update_stale_flag()`/`_write_info_json()`
      (`camera_pipeline.py`) — i.e. `info.json` is only rewritten when the
      flag's value actually changes, not every frame/window.
- [x] A rate-limited log warning fires when `frame_dark` transitions to
      `True` (and optionally when it clears back to `False`).
- [x] The signal is **purely advisory**: no existing code path (daemon RPC
      handlers, MCP tool handlers, calibration logic) is modified to gate,
      block, or refuse any operation based on `frame_dark`. This matches
      the confirmed-advisory behavior of the existing `calibration_stale`
      field (verify via grep that no new gating is introduced, mirroring
      the audit already done for `calibration_stale` during planning).
- [x] No MCP tool surface change in this ticket (no new field on
      `open_camera`'s response, no new MCP tool). This is intentionally
      deferred — see Open Question 3 in `architecture-update.md`.
- [x] The luminance-floor and window-size constants are clearly named and
      isolated (e.g. module-level constants or a small config block in
      `camera_pipeline.py`) so they can be tuned later without a structural
      change.
- [x] Exposure/gain remediation is NOT implemented in this ticket. If
      implementation surfaces a trivially-safe opportunistic improvement,
      it must be raised as a follow-up issue rather than folded in here.
- [x] `uv run pytest` green; no regression in existing daemon pipeline
      tests.

## Testing

- **Existing tests to run**: `tests/test_daemon_server.py`, any existing
  `camera_pipeline.py`-focused tests (grep for `camera_pipeline` in
  `tests/`), `tests/test_daemon_protocol.py`.
- **New tests to write**:
  - Synthetic-dark-frames test: feed the luminance/zero-tag check a
    sequence of dark frames with zero tag detections spanning the
    configured window; assert `frame_dark` becomes `True` and `info.json`
    is written exactly once at the transition.
  - Synthetic-bright-frames / nonzero-detections test: assert `frame_dark`
    stays `False` (or clears) when either luminance is adequate or tags are
    detected.
  - Write-on-change test: assert `info.json` is NOT rewritten on every
    frame/window when `frame_dark`'s value hasn't changed (mirrors the
    existing `_maybe_update_stale_flag` test pattern, if one exists — reuse
    it as a template).
- **Verification command**: `uv run pytest`

## Implementation Plan

**Approach**: extend the existing per-pipeline health-tracking logic in
`camera_pipeline.py` (the same loop that already tracks
`consecutive_failures`) with a new, independent windowed check. Reuse the
existing `info.json` write-on-change infrastructure rather than introducing
a new file or write path.

**Files to create/modify**:
- `src/aprilcam/daemon/camera_pipeline.py` — add the luminance/zero-tag
  windowed check, the `frame_dark` state, and wire it into the existing
  `_write_info_json()`/write-on-change call site.
- `tests/test_daemon_frame_dark_signal.py` (new, or added to an existing
  `camera_pipeline`-focused test file if one exists) — the tests described
  above.

**Testing plan**: unit tests against the pipeline's frame-processing
function with synthetic (in-memory NumPy) frames — no live camera or
hardware required.

**Documentation updates**: note the new `info.json` field
(`frame_dark`) alongside the existing documented `calibration_stale` field,
if `info.json`'s schema is documented anywhere (check
`docs/knowledge/` or `ROBOT_API_GUIDE.md`); otherwise not required for
acceptance.
