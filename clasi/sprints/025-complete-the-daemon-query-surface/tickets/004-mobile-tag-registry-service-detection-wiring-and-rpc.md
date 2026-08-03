---
id: '004'
title: Mobile-tag registry service, detection wiring, and RPC
status: done
use-cases:
- SUC-002
depends-on:
- '003'
github-issue: ''
issue: complete-the-daemon-query-surface.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Mobile-tag registry service, detection wiring, and RPC

## Description

Implement `register_tag`/`unregister_tag`/`list_tag_parameters` on
`DaemonService` (`daemon/service.py`), wire ticket 003's correction
math into `DetectionEngine` (`daemon/detection.py`) so every consumer
(`get_tag`, `get_tags`, `get_tag_history`) reports the robot's pose,
and add the three new RPCs.

**`DaemonService`** gains `self._mount_params: Dict[TagId,
MountParameters]`, guarded by the existing `self._lock`, alongside
`self._playfields`/`self._camera_configs`/`self._calibrations`/
`self._annotations` — the same "one more dict under the existing lock"
pattern sprint 024 ticket 001 used for the annotation store.

- `register_tag(tag, params)` — upsert by `TagId`. `mount_z` set with
  no calibration present anywhere the tag could be observed -> reject
  (raise `errors.NotCalibrated`, the existing error, reused rather than
  inventing a new one) instead of silently storing a value that will
  never apply (UC-12.S-5). A partial spec (`mount_z` set, `mount_x`/
  `mount_y` unset) is accepted as zero offset — no validation error,
  since `MountParameters`' own fields already default to `0.0`.
- `unregister_tag(tag)` — remove by `TagId`; an absent key is a no-op
  (matches `clear_annotations`'s established no-op-on-absent
  convention from sprint 024).
- `list_tag_parameters()` — return a snapshot `dict[TagId,
  MountParameters]` of current registrations.

**`DetectionEngine.__init__`** (`daemon/detection.py`) gains
`mount_lookup: Callable[[TagId], Optional[MountParameters]] =
_default_mount_lookup` (a no-op default returning `None` for every tag,
mirroring `_default_calibration_lookup`/`_default_playfield_lookup`'s
existing shape exactly at lines 252-268 of that module) — additive, so
every existing caller/test that does not pass it is unaffected.

Apply the correction inside `run_burst`'s per-record path, immediately
after `_build_tag_record` populates `world`/`world_corners`/`yaw_rad`
from calibration (`detection.py` lines 317-375): when
`mount_lookup(tag.tag)` returns a non-`None` `MountParameters`, replace
`world`/`yaw_rad` with ticket 003's corrected values before the record
enters the ring buffer via `_append`. This guarantees **every**
consumer of the buffer — `get_tag`, `get_tags`, `get_tag_history`, and
the not-yet-built `stream_tags` (sprint 027) — sees the robot's pose
for free, with the correction applied exactly once, never duplicated
at each call site.

**Not retroactive** (ticket 003's use-case-doc resolution, implemented
here): correction applies only to records built *after* a registration
exists; already-buffered `TagFrame`s are never recomputed.
`register_tag`/`unregister_tag` touch only `self._mount_params`, never
the ring buffer.

**Default tag size (UC-12.S-1)**: `register_tag` accepts
`MountParameters.size_cm` exactly as given (may be `None`); no
daemon-level default substitution this sprint (`sprint.md`'s Design
Rationale decision 4) — `list_tag_parameters` reports exactly what was
registered, nothing fabricated.

**Proto**: three new RPCs —
`RegisterTag(RegisterTagRequest{TagId tag; MountParameters params;})
returns (RegisterTagResponse{})`,
`UnregisterTag(UnregisterTagRequest{TagId tag;}) returns
(UnregisterTagResponse{})`,
`ListTagParameters(ListTagParametersRequest{}) returns
(ListTagParametersResponse{repeated TagMountEntry entries;})` where
`TagMountEntry{TagId tag; MountParameters params;}`. Add a new
`MountParameters` message mirroring `types.MountParameters`
field-for-field (`optional double size_cm`, `double mount_x`/
`mount_y`/`mount_z`/`mount_yaw_rad`), matching this file's existing
`optional`-for-legitimate-absence discipline.

**`daemon/rpc.py` + `client/daemon_client.py`**: three new dispatch
methods / stub methods, mirroring the existing pattern exactly
(marshalling only).

## Acceptance Criteria

- [x] `register_tag`/`unregister_tag`/`list_tag_parameters`
      implemented on `DaemonService`, held under the existing lock.
- [x] UC-12.S-5: registering `mount_z` on a tag with no calibrated
      camera anywhere is rejected, never silently stored-but-inert.
- [x] Partial mount spec (`mount_z` set, `mount_x`/`mount_y` unset) is
      accepted, treated as zero offset.
- [x] A registration for a tag not currently visible is accepted and
      takes effect once the tag is detected.
- [x] `DetectionEngine` gains `mount_lookup`, defaulting to a no-op —
      existing `test_detection.py` tests pass unmodified (proving the
      default is truly inert).
- [x] A synthetic detection test (fixture-backed camera, known
      corrected-vs-raw expected positions from ticket 003's math)
      proves `get_tag`/`get_tags`/`get_tag_history` all report the
      corrected pose once a mount is registered, and the raw pose once
      unregistered.
- [x] Mount-parameter changes are confirmed non-retroactive by test:
      buffered history recorded before a registration keeps the
      pre-registration (raw) values when re-fetched via
      `get_tag_history` after the registration is added.
- [x] `RegisterTag`/`UnregisterTag`/`ListTagParameters` RPCs
      round-trip correctly through a real gRPC channel.
- [x] `uv run mypy` clean; `MountParameters` stays a frozen
      `@dataclass(kw_only=True)` — no `typing.Protocol` introduced
      anywhere in the diff.
- [x] Full headless suite green (baseline 795 headless tests, this
      ticket only adds).

## Implementation Plan

**Approach**: registry + rejection logic in `DaemonService` first
(pure Python, testable via direct construction per
`test_service.py`'s existing pattern), then `DetectionEngine`'s
`mount_lookup` injection point (unit-testable via the existing
fixture-replay pattern `test_detection.py` already uses for
`calibration_lookup`/`playfield_lookup`), then proto + RPC + client
stub last (thinnest layer, needs the service methods to exist first).

**Files**: `src/aprilcam/daemon/service.py` (extend),
`src/aprilcam/daemon/detection.py` (extend `DetectionEngine.__init__`,
`run_burst`), `proto/aprilcam_v2.proto` (extend), regenerated stubs,
`src/aprilcam/daemon/rpc.py` (extend),
`src/aprilcam/client/daemon_client.py` (extend).

**Testing plan**:
- **Existing tests to run**: `uv run pytest
  tests/aprilcam/daemon/test_service.py
  tests/aprilcam/daemon/test_detection.py
  tests/aprilcam/client/test_daemon_client.py -v`, plus the full
  headless suite — `test_detection.py`'s existing tests must pass with
  zero changes, proving `mount_lookup`'s default is a true no-op.
- **New tests to write**: registry unit tests (register/unregister/
  list, rejection on uncalibrated `mount_z`, partial-spec zero-offset,
  unseen-tag acceptance) against `DaemonService` directly;
  `DetectionEngine` tests with a non-default `mount_lookup` proving
  corrected records enter the buffer and are retrievable via
  `get_tag`/`get_tags`/`get_tag_history`; a non-retroactive-history
  test; a gRPC round-trip test for the three new RPCs.
- **Verification command**: `uv run pytest -m "not camera"` and
  `uv run mypy`.

**Documentation updates**: none beyond docstrings — decisions already
recorded in ticket 003 / `sprint.md` / UC-12.
