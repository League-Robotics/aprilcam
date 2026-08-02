---
id: '002'
title: Self-describing config template and resolution key
status: done
use-cases:
- SUC-002
depends-on:
- '001'
github-issue: ''
issue: config-service-completion-and-annotations.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Self-describing config template and resolution key

## Description

`Camera.effective_config()` currently reports only `device_name`/
`frame_width`/`frame_height` as a placeholder — `camera/camera.py`'s own
docstring says the fuller "every settable key, blank when unset"
template (UC-03.S-1) "needs real per-model UVC-control enumeration this
sprint does not implement." Ticket 001 lands the real settings-apply
mechanism (OpenCV or `uvc-util`); this ticket makes `effective_config`
report that mechanism's real settable-key set instead of a hardcoded
pair, plus makes `frame_width`/`frame_height` a genuinely settable key
(not just introspected) so resolution round-trips through push/dump
like every other setting.

This is the document every camera configuration starts from
(UC-03.S-1, UC-25) — the template `camera config --dump` (ticket 005)
returns for an unconfigured camera (UC-24.S-3), so its accuracy matters
beyond this ticket alone.

## Acceptance Criteria

- [x] `Camera.effective_config()` reports every settable key the active
      backend(s) actually expose for this camera, each present and
      blank (`None`) when unset — not a fixed placeholder list.
- [x] `frame_width`/`frame_height` is a settable key: present in the
      template, and a pushed value is applied via `OpenCVBackend`'s
      existing `CAP_PROP_FRAME_WIDTH`/`CAP_PROP_FRAME_HEIGHT` path.
- [x] UC-03.S-1 verified live: fetching a freshly-enumerated,
      never-configured camera's config returns introspected facts
      (device name, frame dimensions) plus the full settable-key set
      for *that* camera, each blank.
- [x] Two different camera models (camera 3 / `hd-usb-camera`) report
      different settable-key sets if their backends genuinely differ,
      or the same set with a documented reason if not.
- [x] `uv run mypy` clean; baseline 629 passed / 1 skipped stays green;
      zero `typing.Protocol`.

## Implementation Plan

**Files to modify:**
- `src/aprilcam/camera/camera.py` — `effective_config`: replace the
  two-key placeholder dict with a real enumeration from the backend(s)
  in use (ticket 001's mechanism).
- `src/aprilcam/camera/backends/opencv_backend.py` — `property_map()`:
  add `frame_width`/`frame_height` mapped to
  `CAP_PROP_FRAME_WIDTH`/`CAP_PROP_FRAME_HEIGHT`.
- `src/aprilcam/camera/backends/uvc_util.py` (if it exists from ticket
  001) — ensure its `settable_names()` contributes to the merged set
  `effective_config` reports.

**Archive-reuse pointers:** none beyond ticket 001's.

## Testing

- **Existing tests to run**: `tests/aprilcam/camera/test_camera.py`,
  full `uv run pytest` for the 629/1 baseline.
- **New tests to write**: headless — mocked-backend test asserting the
  template includes every key the mock backend's `settable_names()`
  reports, each blank. `@pytest.mark.camera` — UC-03.S-1 against
  camera 3, asserting the real key set (exposure, gain, white balance,
  resolution, etc., per whatever ticket 001 landed).
- **Verification command**: `uv run pytest`; `uv run pytest -m camera`;
  `uv run mypy`.
