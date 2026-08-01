---
id: '002'
title: Resolve cameras by persistent number, daemon-side
status: done
use-cases:
- SUC-005
depends-on: []
github-issue: ''
issue: cameras-referenceable-by-persistent-number.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Resolve cameras by persistent number, daemon-side

## Description

`camera list` prints each camera's stable registry number, but typing
it back (`camera image 3`) fails with "unknown camera '3'" —
`DaemonService._camera_for` (`src/aprilcam/daemon/service.py:257`ish)
matches on slug only. The persistence half is already built and
working (sprint 020): `CameraRegistry` assigns a `number` once, never
reshuffles it, and `CameraState.number` already carries it to clients.
Only the addressing layer is missing. Fix it once, daemon-side, in
`_camera_for` — every adapter (CLI today, MCP later, this sprint's new
detection RPCs) inherits it automatically, per `adapters.md` §2's
duplication test.

This ticket's fix lands on the **existing** `GetFrame`/`ListCameras`/
`ProbeCameras` RPCs too, since `get_frame` already calls
`_camera_for`. Do not defer the wire-level error-mapping half of this
work to a later ticket — an unmapped new exception type would
immediately regress `GetFrame`'s existing typed-error contract
(`daemon/rpc.py`'s `_abort`, `client/daemon_client.py`'s
`_reconstruct_error`).

## Acceptance Criteria

- [x] `CameraRegistry.get_by_number(number: int) -> Optional[CameraRecord]`
      exists alongside the existing `get(slug)`.
- [x] `_camera_for` accepts either form: an all-digits token resolves
      via `get_by_number`; anything else resolves via slug, exactly as
      today. No ambiguity is possible — confirm and state as an
      invariant in `camera/identity.py` (near `device_name_slug`/
      `name_resolution_slug`) that every derived slug contains at
      least one non-digit character, including the last-resort
      name+resolution fallback tier, so an all-digits token can never
      collide with a real slug.
- [x] A number matching no registered camera raises `UnknownCamera`
      whose `available` tuple is built from entries formatted
      `"{number} {slug}"` (falling back to bare `slug` for any record
      somehow missing a number) — the error teaches the addressing
      scheme instead of just rejecting it.
- [x] A **registered but not currently enumerated** camera — addressed
      by either number or slug — raises a new `CameraNotPresent`
      error (add to `src/aprilcam/errors.py`, alongside the other
      `AprilCamError` subclasses, carrying the slug), distinct from
      `UnknownCamera`. This replaces `_camera_for`'s current behavior
      of raising `UnknownCamera` for both "never registered" and
      "known but not present right now" — the two cases must now be
      distinguishable, for both addressing forms, not just number
      addressing (the same `_camera_for` code path serves both).
- [x] `daemon/rpc.py`'s `_STATUS_CODES` maps `CameraNotPresent` to a
      gRPC status code (recommend `FAILED_PRECONDITION`, matching
      `NotCalibrated`'s "the operation cannot succeed given current
      state" semantics) and `_abort` carries the slug in trailing
      metadata; `client/daemon_client.py`'s `_reconstruct_error`
      rebuilds it client-side.
- [x] `docs/design/use-cases/cli.md`'s addressing-conventions text
      (near UC-14) documents the number-or-slug scheme explicitly —
      "a decimal number resolves the camera holding that number;
      anything else resolves by slug" — so an agent reading only
      slugs today would discover numbers exist. (The issue also asks
      for this in "the packaged guide" — no `guides.py`/
      `AGENT_GUIDE.md` exists in the v2 tree yet, so that half is
      deferred until that subsystem is built; do not invent it here.)
- [x] `uv run mypy` stays clean; `uv run pytest` stays green.

## Testing

- **Existing tests to run**: `uv run pytest tests/aprilcam/camera/
  tests/aprilcam/daemon/ -v`, full suite, `uv run mypy`.
- **New tests to write**:
  `tests/aprilcam/camera/test_registry.py` — `get_by_number` round
  trips against a freshly-resolved record; returns `None` for an
  unassigned number.
  `tests/aprilcam/daemon/test_service.py` (extend the existing
  `held_camera`/`service` fixtures) — `get_frame("3")` and
  `get_frame(<slug>)` on the same held camera return frames tagged
  identically; `get_frame("99")` raises `UnknownCamera` with
  `available` containing `"N slug"`-formatted entries; a camera
  present in the registry but absent from `identity_source()`'s
  return (simulating unplugged) raises `CameraNotPresent`, not
  `UnknownCamera`, addressed both by slug and by number.
  RPC round-trip test (extend the `stub` fixture pattern) —
  `CameraNotPresent` maps to the chosen status code with the slug in
  trailing metadata, and the client reconstructs it.
- **Verification command**: `uv run pytest tests/aprilcam/camera/
  tests/aprilcam/daemon/ -v && uv run mypy`

## Implementation Plan

### Approach

1. `camera/registry.py`: add `get_by_number(number)` mirroring the
   existing `get(slug)` (linear scan over `self._records.values()` —
   the registry is small; no new index structure needed).
2. `camera/identity.py`: add a one-line invariant note (docstring or
   assertion in `_slug`) that a slug is never all-digits — `_slug`'s
   regex already strips non-alphanumerics and a bare numeric device
   name would slip through; verify this against `name_resolution_slug`
   specifically (the last-resort tier) and, if a bare-numeric slug is
   actually reachable, guard against it there (e.g. prefix with
   `camera-`) rather than merely asserting.
3. `errors.py`: add `class CameraNotPresent(AprilCamError)` — carries
   `slug`, message along the lines of `f"camera {slug!r} is
   registered but not currently present"`. Follow the existing style
   (`UnsupportedSetting`, `NotCalibrated`) — a small `__init__`
   setting the field before calling `super().__init__(...)`.
4. `daemon/service.py`: rework `_camera_for` — parse the token
   (`token.isdigit()` → number path via `get_by_number`; else slug
   path via `get(slug)`), build the `available` tuple as
   `f"{r.number} {r.slug}"` for every record, and where the current
   code raises `UnknownCamera` for "found the record but couldn't
   locate an index among enumerated backends," raise
   `CameraNotPresent(record.slug)` instead — reserve `UnknownCamera`
   for "no record for this token at all."
5. `daemon/rpc.py`: add `CameraNotPresent` to `_STATUS_CODES`; extend
   `_abort`'s metadata-building `if/elif` chain with a
   `CameraNotPresent` branch (`aprilcam-slug` metadata key, documented
   next to the existing ones per that module's own convention).
6. `client/daemon_client.py`: extend `_reconstruct_error` to rebuild
   `CameraNotPresent` from the new metadata key.
7. `docs/design/use-cases/cli.md`: add the addressing-scheme sentence
   near UC-14's main success path or preconditions.

### Files to create/modify

- `src/aprilcam/camera/registry.py` (modify — add `get_by_number`)
- `src/aprilcam/camera/identity.py` (modify — invariant note/guard)
- `src/aprilcam/errors.py` (modify — add `CameraNotPresent`)
- `src/aprilcam/daemon/service.py` (modify — `_camera_for`)
- `src/aprilcam/daemon/rpc.py` (modify — `_STATUS_CODES`, `_abort`)
- `src/aprilcam/client/daemon_client.py` (modify — `_reconstruct_error`)
- `docs/design/use-cases/cli.md` (modify — addressing conventions)
- `tests/aprilcam/camera/test_registry.py` (modify)
- `tests/aprilcam/daemon/test_service.py` (modify)

### Documentation updates

`docs/design/use-cases/cli.md` as above. No architecture doc changes
needed beyond this sprint's `sprint.md` (already written).
