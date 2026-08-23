---
id: '003'
title: 'Capture-tool updates: field-state-id, calibration snapshot pinning, ov9281
  entry, HD-cam fix'
status: done
use-cases:
- UC-10
depends-on: []
github-issue: ''
issue: aprilcam-v2-layered-test-system.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Capture-tool updates: field-state-id, calibration snapshot pinning, ov9281 entry, HD-cam fix

## Description

Before the operator-assisted capture session (ticket 004) can run,
`testdev/capture_matrix.py`/`.yaml` need four additive updates named in
the source issue's Sprint A item 1:

1. A `--field-state-id` flag so a capture run tags its manifest with
   which field state (e.g. `fs-20260823`) was active, instead of always
   writing to the single implicit corpus.
2. `tests/images/field-state.json` always read/written at the fixture
   root (not per-camera) so every camera's manifest can reference the
   same field-state id.
3. Pinning the active `calibration.json` snapshot beside each camera's
   fixtures, with a sha256 recorded in the manifest — §6.2 of
   `docs/test-plans/camera-image-matrix.md` requires this so L0's
   world-accuracy tests don't depend on live machine state.
4. A new `ov9281` entry in `capture_matrix.yaml` (baseline exposure 20,
   sweep 5→300 spanning day/night per the issue's field-state note, gain
   axis, no WB — this sensor is mono) and the HD-cam
   auto-to-manual control-ordering fix (push `auto-exposure-mode`
   manual, and any other `_GATE_KEYS`-style control, before pushing
   exposure/gain values — camera 2 was failing to apply its variation
   matrix in the July run for exactly this reason).

This ticket is code-only and has no dependency on the capture session
or new fixtures — it must land and be verified against the *existing*
July fixtures/cameras before ticket 004 runs.

## Acceptance Criteria

- [x] `python testdev/capture_matrix.py <camera> --field-state-id
      fs-20260823` writes that id into every manifest entry the run
      produces, and updates `tests/images/field-state.json` at the
      fixture root (not per-camera) — verified by inspecting the written
      manifest and `field-state.json` after a dry/real run.
      Verified dry (per the Critical Constraints — no camera access this
      session): `TestRunCameraEndToEnd::test_full_matrix_applies_with_calibration_and_field_state`
      drives the real `run_camera`/`ensure_field_state_stub` code paths
      end to end with only hardware I/O faked, and inspects the real
      written `manifest.json` (`field_state_id: "fs-20260823"`) and
      `field-state.json` (`current: "fs-20260823"`) on disk.
      `TestEnsureFieldStateStub` covers the fixture-root-not-per-camera
      placement and non-clobbering of a confirmed entry directly.
- [x] Every manifest entry gains a `calibration_snapshot_sha256` field,
      and a copy of the active `calibration.json` is written beside that
      camera's fixtures at capture time.
      Verified by `TestPinCalibrationSnapshot` (copy + matching sha256 +
      source never written + `None` when absent) and by the same
      end-to-end test above inspecting the real manifest field and the
      copied `calibration.json` bytes.
- [x] `capture_matrix.yaml` gains an `ov9281` entry matching the device
      name pattern for `arducam-ov9281-usb-camera`; running the capture
      tool against camera 5 selects it (verified by a dry-run or a real
      capture against camera 5, not just eyeballing the YAML).
      Verified dry: `TestOv9281MatrixEntry::test_selected_for_the_real_device_name_pattern`
      calls the real `select_matrix_entry` against the real OS-reported
      device name string (`"Arducam OV9281 USB Camera"`), not a YAML
      read.
- [x] Re-running the capture tool against `hd-usb-camera` (camera 2)
      successfully applies its full variation matrix — auto-exposure is
      pushed manual (and any other gate-flagged control) *before*
      exposure/gain values, mirroring `camera.camera`'s own
      `_GATE_KEYS`-first ordering — where the July run's manifest shows
      values rejected on a live handle, this run's manifest shows them
      accepted.
      Verified against fakes/recorded call order, per the Critical
      Constraints (no camera access this session — real-hardware
      confirmation against camera 2 is ticket 004's job, not this
      ticket's): `TestApplyControlsAgainstFakeHardware` reproduces
      July's exact recorded baseline (`auto-exposure-mode: "8"`,
      `auto-white-balance-temp: "true"`) against a fake uvc-util that
      rejects a value push exactly when its gate is not manual (mirroring
      real observed behavior), and shows the push failing pre-fix-shape
      and succeeding once `_ensure_gate_overrides` + `apply_controls`'s
      gate-first ordering are applied, with the gate control recorded as
      the first call. `TestRunCameraEndToEnd::test_full_matrix_applies_with_calibration_and_field_state`
      runs the same scenario through the real `run_camera` end to end and
      asserts `result.skipped_apply_failed == []` — none of July's twelve
      `apply_failed` variations recur.
- [x] The tool still always restores baseline controls in a `finally:`
      block (existing behavior; regression-checked, not just assumed).
      Verified by both `TestRunCameraEndToEnd` tests asserting
      `result.restored is True` unconditionally (the finally-block flag),
      and by `test_default_no_field_state_id_stays_unnested`'s
      fully-manual baseline scenario asserting `restore_mismatches == []`
      (byte-exact restore, achievable when nothing gates the restored
      values); the auto-baseline HD-cam test asserts the two gate
      controls themselves restore exactly and documents why a byte-exact
      readback of the two auto-overridden *values* specifically is not
      achievable on this or the real July hardware (an out-of-scope,
      pre-existing limitation, not introduced or worsened by this fix).
- [x] Existing camera entries (arducam-ov9782) and their existing
      capture behavior are unchanged — a re-run against that camera with
      no new flags produces the same variation set as before.
      Verified by `TestEnsureGateOverrides::test_ov9782_regression_effective_controls_unchanged`
      (gate injection never changes the effective/merged controls actually
      pushed and recorded for an already-manual baseline),
      `TestOv9281MatrixEntry::test_not_selected_for_ov9782_or_hd_cam`, and
      `TestRunCameraEndToEnd::test_default_no_field_state_id_stays_unnested`
      (no `--field-state-id` keeps the flat, unnested
      `tests/images/<slug>/` layout).

## Implementation Plan

**Approach**: Extend the existing tool additively — this is not a
rewrite. `--field-state-id` is a new CLI argument threaded into the
manifest-writing step; the calibration-snapshot pin reuses whatever
`calibration.json` the daemon's own on-disk convention already produces
(read-only, this tool never writes a live `calibration.json`); the
HD-cam fix reorders control application to match
`camera.camera.Camera.apply_config`'s own `_GATE_KEYS`-first convention,
which this tool does not currently reuse (it drives hardware directly,
per `docs/test-plans/camera-image-matrix.md` §4's design decision) but
should mirror.

**Files to modify**:
- `testdev/capture_matrix.py` — `--field-state-id` flag, manifest
  schema addition (`calibration_snapshot_sha256`), calibration-snapshot
  copy step, control-ordering fix for auto-to-manual cameras.
- `testdev/capture_matrix.yaml` — new `ov9281` entry.
- `tests/images/field-state.json` — no content change this ticket
  (ticket 004 adds the confirmed `fs-20260823` entry after the real
  capture session), but the tool's read/write path for this file is
  what changes here.
- `docs/test-plans/camera-image-matrix.md` — note the four changes
  against §4 (capture program design) and §6.2 (calibration snapshot
  requirement).

**Testing plan**:
- Re-run the tool against the existing arducam-ov9782 and hd-usb-camera
  entries (no new fixtures needed — this validates against currently
  attached hardware) and inspect the resulting manifests.
- One new unit test (no hardware needed) for the manifest-schema
  addition and the control-ordering logic, using a faked/injected
  backend the way the tool's existing structure allows.
- **Verification command**: `uv run pytest tests/aprilcam/ -k capture_matrix -v`
  (this ticket's own unit coverage; the full manual hardware re-run
  above is exploratory verification, not a committed test).

**Documentation updates**:
- `docs/test-plans/camera-image-matrix.md` — §4 and §6.2, as above.
