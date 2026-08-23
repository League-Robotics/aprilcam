---
id: '007'
title: Port testdev/capture_matrix.py onto the v2 surface
status: done
use-cases:
- SUC-007
depends-on: []
github-issue: ''
issue:
- capture-matrix-tool-imports-deleted-modules.md
- hardening-cleanup-and-the-design-doc-set.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Port testdev/capture_matrix.py onto the v2 surface

## Description

`testdev/capture_matrix.py` fails to import: it depends on five modules
deleted or restructured in the v2 rebuild
(`aprilcam.camera.camutil`, `aprilcam.camera.camera_config`,
`aprilcam.camera.registry.dir_slug`, `aprilcam.config.Config`,
`aprilcam.core.detector`). This is the tool that regenerates
`tests/images/`'s fixture matrix, which `docs/design/design.md` §8
describes as "regenerable, not sacred" — a claim that is currently
false because the regenerator is broken.

Port the five now-missing imports onto their v2 equivalents:
`camera/identity.py` + `camera/registry.py` for identity/slugs,
`camera/camera.py` + `camera/backends/` for capture/settings,
`config/directories.py` for roots, and `vision/detector.py` if the tool
wants detection annotations. The tool's own contract is unchanged: sweep
a device-name-matched variation matrix, always restore baseline
controls (even on failure — `finally` block), write
`tests/images/<camera-slug>/manifest.json` recording effective
controls, camera identity, resolution, playfield, capture timestamp,
package version, and field-state id.

**Design decision — direct-open, not daemon RPC** (already made and
recorded in `sprint.md`'s Architecture section; this ticket implements
it, and should also record it in the tool's own module docstring):
`SetCameraConfig`/`GetFrame` are whole-document push/single-frame-read
RPCs with no shape for "apply one raw UVC control in isolation, capture,
restore baseline, repeat per matrix row" — the tool's actual operation.
Building that RPC shape would serve exactly one caller and would break
the tool's documented ability to run standalone before a daemon exists
or while one is down for maintenance (`design.md` §8's sanctioned
exception for offline test tooling). Keep the tool's existing
direct-open contention check (refuses to run when the daemon holds the
device) unchanged.

## Acceptance Criteria

- [x] `uv run python testdev/capture_matrix.py --help` runs without an
      import error.
- [x] A live capture run (hardware-gated — exercised manually or in a
      hardware-marked test, per this project's testing conventions)
      against at least one attached camera reproduces the documented
      `tests/images/<camera-slug>/manifest.json` layout: effective
      controls, camera identity, resolution, playfield, capture
      timestamp, package version, field-state id.
- [x] Baseline controls are restored in a `finally` block after every
      run (success or failure), verified by reading them back where the
      backend supports read-back — matching the tool's pre-existing
      contract.
- [x] The tool's existing daemon-contention refusal (will not run while
      the daemon holds the device) still works against the v2 daemon.
- [x] The direct-open-vs-RPC design decision (see Description) is
      recorded in the tool's own module docstring, not only in
      `sprint.md`.
- [x] `uv run mypy testdev/capture_matrix.py` (or equivalent scoped
      check, if `testdev/` is outside the package's normal mypy scope —
      match this project's existing convention for that directory)
      clean.

## Implementation Plan

**Approach**: work through the five broken imports one at a time,
mapping each v1-era name to its v2 replacement per the Description
above, and re-read the ported functions against the tool's existing
contract (documented in its own module docstring, already read during
sprint planning: verify-no-daemon-contention, identify camera, read own
baseline (never hardcoded), select variation matrix by device-name
pattern from `capture_matrix.yaml` or fall back to a generic sweep,
capture per variation, always restore, write manifest). Do not change
the tool's external behavior or CLI surface (`--all`, `<pattern>`,
`--dry-run`) — this is a dependency repair, not a redesign.

**Files**:
- `testdev/capture_matrix.py` — the import repairs and any call-site
  adjustments the new v2 APIs require (e.g. `camera/registry.py`'s
  `CameraRegistry` API differs from v1's `dir_slug` helper — read the
  current `registry.py` to find the equivalent).
- `testdev/capture_matrix.yaml` — check whether any device-name pattern
  keys need updating for v2's camera slugs (likely unchanged, but
  verify against `camera/identity.py`'s current slug derivation).

**Testing plan**:
- **Existing tests to run**: none exist for `testdev/` today (it is
  offline tooling, not part of the `tests/` package) — this ticket's
  own verification is the `--help`/live-capture acceptance criteria
  above.
- **New tests to write**: none required in `tests/` (per this tool's
  existing convention of being verified by direct invocation, not
  pytest) — however, if the implementer finds a clean way to add a
  lightweight import-smoke-test (e.g. `python -c "import
  testdev.capture_matrix"` wrapped in a headless pytest case) without
  requiring hardware, that is a welcome addition, not a requirement.
- **Verification command**: `uv run python testdev/capture_matrix.py
  --help`, then a hardware-gated live run against at least one attached
  camera.

**Documentation updates**: `testdev/capture_matrix.py`'s own module
docstring (the direct-open-vs-RPC decision, per Acceptance Criteria).

## Completion Notes

- Ported the five broken imports onto `camera/identity.py` (`resolve_all`),
  `camera/registry.py` (`CameraRegistry`, read-only from this tool's side),
  `camera/backends/opencv_backend.py` (`OpenCVBackend`, replacing raw
  `cv2.VideoCapture`), `config/directories.py` (`Directories`), and
  `vision/detector.py` (`TagDetector`/`DetectorConfig`).
- Camera discovery (`discover_target_cameras`) is now registry-mediated
  rather than device-name-string matching against `config.json`: v2's
  self-naming `CameraConfig` carries no `device_name` field at all, so a
  configured slug is now reconciled against the live enumeration through
  the registry's `hardware_key`, never mutating `registry.json`.
- Baseline-control snapshotting (`snapshot_baseline`) now always reads the
  live hardware state rather than ever trusting `config.json`: v2's
  `CameraConfig.settings` is keyed by `Camera.apply_config`'s abstracted
  vocabulary (`exposure`, `gain`, ...), not the raw `uvc-util` control
  names (`exposure-time-abs`, ...) this tool's per-control sweep needs —
  the on-disk `settings.controls` block the old branch trusted no longer
  exists in any v2 config. This is a strictly stronger reading of the
  tool's own pre-existing "never a hardcoded baseline" contract, not a
  behavior regression.
- `check_daemon_not_running` now reuses `client.discovery.probe_lock_holder`
  against `directories.socket_dir / daemon.lifecycle.LOCK_FILENAME` —
  the exact same non-blocking-flock probe `client/lifecycle.py`'s own
  `start()` pre-flight check uses — instead of reimplementing flock
  logic against a v1-shaped `Config.daemon_lock_path`/`daemon_pidfile`
  pair that no longer exists.
- v1's `camutil.diagnose_camera_failure` (OS-level "who is holding this
  device" diagnosis) has no v2 counterpart anywhere in `camera/`; the
  separate per-camera busy pre-check was dropped in favor of relying on
  `OpenCVBackend.open()`'s own failure (after the daemon-lock check has
  already ruled out the one cause this tool can name with confidence) —
  documented in the module docstring rather than silently narrowed.
- Direct-open-vs-daemon-RPC decision recorded in the module docstring
  (already made in `sprint.md`'s Architecture section; not re-decided
  here) — this ticket only implements and documents it in the tool
  itself, per the acceptance criterion.
- `capture_matrix.yaml` needed no changes: matrix-entry matching is
  against the live `device_name` string, which v2's identity resolution
  still reports unchanged (e.g. "Arducam OV9782 USB Camera") — slugs
  never enter that matching path.
- `uv run mypy testdev/capture_matrix.py` alone reports only
  import-resolution noise for every `aprilcam.*` import (`testdev/` is
  outside `[tool.mypy]`'s `files = ["src/aprilcam"]`, so mypy treats the
  editable-installed, `py.typed`-less `aprilcam` package as an untyped
  third-party dependency when checking a file outside that scope, per
  mypy's own module-resolution rules) — this is a resolution-context
  artifact, not a defect in this file. The equivalent scoped check,
  `MYPYPATH=src uv run mypy testdev/capture_matrix.py`, resolves
  `aprilcam.*` as first-party (matching how the project's own
  `tests/test_typing.py` gate checks `src/aprilcam`) and is clean. That
  run surfaced one genuine, pre-existing gap `testdev/` was never
  checked against before (`import yaml` with no installed stubs);
  fixed by adding `types-PyYAML` to `[dependency-groups].dev` in
  `pyproject.toml` (PyYAML has real stubs on PyPI, so this follows the
  project's own documented policy of installing real stubs rather than
  adding a per-module `ignore_missing_imports` override, which that
  section's docstring reserves for libraries with no stubs available at
  all). The project-wide gate (`uv run mypy`, no args, `src/aprilcam`
  only) remains clean — 79 source files, unaffected by this ticket.
- Live-hardware verification: with the local daemon running, the tool
  correctly refused (`REFUSING: the aprilcam daemon appears to be
  running (pid ..., lock held at .../daemon.lock)`, exit 2). With the
  daemon stopped, a real capture run against
  `arducam-ov9782-usb-camera` (pattern `arducam-ov9782`, output
  redirected to a scratch dir, not `tests/images/`) produced 19 images
  and a `manifest.json` matching the documented layout exactly —
  `unique_id` resolved to `avf:0x21141400c456366`, byte-identical to
  the value already committed in `tests/images/arducam-ov9782-usb-camera/
  manifest.json` from the original 2026-07-29 capture, confirming v2's
  identity resolution names the same physical camera. `restored: True,
  mismatches: none` in the run summary confirms the `finally`-block
  restore-and-verify path. The daemon was restarted afterward to leave
  the environment as found. `tests/images/` itself was not touched by
  this ticket's verification (test output went to a scratch directory).
  Nothing remains hardware-gated beyond what already ran here — both
  live-hardware acceptance criteria were exercised against real
  hardware in this session, not just described.
