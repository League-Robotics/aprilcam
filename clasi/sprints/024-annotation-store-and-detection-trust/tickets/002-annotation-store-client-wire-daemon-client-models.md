---
id: '002'
title: "Annotation store \u2014 client wire (daemon_client, models)"
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
depends-on:
- '001'
github-issue: ''
issue: annotation-store-for-shared-spatial-state.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Annotation store — client wire (daemon_client, models)

## Description

Build the client-side half of the wire boundary for ticket 001's five
RPCs — request marshalling in `client/daemon_client.py`, response
marshalling in `client/models.py` — completing `GrpcDaemon`'s
implementation of `Daemon`'s five annotation methods. This mirrors
sprint 020 ticket 005's own split from ticket 004: server and client
wire are separate tickets in this codebase's established convention
(`client/daemon_client.py`'s own module docstring: "request
marshalling lives here; response marshalling does not... every
response crosses back into Python exclusively through
`client/models.py`'s `from_proto` constructors").

**Depends on ticket 001** — the proto messages and RPCs must exist
before this ticket's request builders and converters can be written
against them.

No archive reuse (same reasoning as ticket 001 — new v2 code against
an already-declared interface).

Builds:

- **`client/daemon_client.py`** — five new `GrpcDaemon` methods
  (`put_annotation`/`remove_annotation`/`replace_layer`/
  `clear_annotations`/`get_annotations`), each building its request
  message and calling `self._call(...)`, then handing the raw response
  to a `client/models.py` converter — this module never reads a
  response field itself (its own docstring's rule). Request-side
  `to_proto` helpers for `Annotation`/`Style`/each shape, duplicated
  (not imported) from `daemon/rpc.py`'s equivalents, matching this
  module's own stated "each side of the proto boundary owns its own
  small conversion" pattern (see its existing
  `_playfield_definition_to_proto`/`_camera_config_to_proto` for the
  precedent to copy).
- **`client/models.py`** — the reverse-direction `from_proto`
  converters: `annotation_from_proto`, `annotation_set_from_proto`,
  one per shape, `style_from_proto`. **Update `image_frame_from_proto`**
  to actually carry `annotations` (today it is hardcoded to the
  `types.py` default with a comment explaining the annotation store
  doesn't exist yet — delete that comment along with the hardcoding,
  not just the hardcoding). Follow the existing `tag_record_from_proto`
  field-for-field reverse-mirror style, including the same
  `HasField()`-vs-`None` discipline this module's docstring states as
  load-bearing throughout.

## Acceptance Criteria

- [x] `GrpcDaemon.put_annotation`/`.remove_annotation`/
      `.replace_layer`/`.clear_annotations`/`.get_annotations` all
      implemented, each a thin request-build-and-call, matching the
      existing methods' shape exactly (no business logic — that stays
      in the daemon, per `adapters.md`'s "adapters hold no
      authoritative state").
- [x] `_reconstruct_error` correctly rebuilds `UnknownPlayfield`
      client-side from a `PutAnnotation`/`GetAnnotations`/etc. RPC
      failure — this mapping already exists (used today by
      `get_playfield`); verify by test that it also fires for the new
      RPCs, not just the ones that established it.
- [x] `image_frame_from_proto` returns a real, populated
      `AnnotationSet` when the wire response has one, and `None` when
      it does not (raw frames) — round-trips exactly what ticket 001's
      server sent, including an empty-but-present set (revision 0)
      staying present, not coerced to `None`.
- [x] A full put → fetch round trip through a real (or
      `test_lifecycle_integration.py`-style) daemon reproduces the
      exact `Annotation`/shape/`Style` sent, field for field —
      including a `Style` with unset (`None`) `color`/`line_width`
      staying `None`, not defaulted, on the way back.
- [x] `uv run mypy` clean; zero `typing.Protocol`; `GrpcDaemon`
      continues deriving from the plain-base-class `Daemon`.
- [x] Full suite green: `uv run pytest -m "not camera"` (baseline 717
      headless).

## Implementation Plan

**Approach**: mirror `client/daemon_client.py`'s existing methods
line-for-line in shape (see `get_calibration`/`set_camera_config` for
the closest analogues — one Optional-returning read, one
plain-request write). Write `client/models.py`'s converters as the
literal field-for-field reverse of ticket 001's `daemon/rpc.py`
converters — if a field is optional there, it round-trips through
`HasField()` here, no exceptions.

**Files**:
- `src/aprilcam/client/daemon_client.py` (extend `GrpcDaemon`)
- `src/aprilcam/client/models.py` (extend, update
  `image_frame_from_proto`)

**Testing plan**:
- **Existing tests to run**: `uv run pytest tests/aprilcam/client/
  test_daemon_client.py -v` plus the full headless suite. Confirm
  `image_frame_from_proto`'s existing tests (if any explicitly assert
  `annotations is None`) are updated to reflect the new real
  round-trip rather than a hardcoded absence, if they exist and would
  otherwise give a false sense of coverage.
- **New tests to write**: unit tests against `GrpcDaemon` with a fake/
  stub gRPC channel (matching `test_daemon_client.py`'s existing
  pattern) for each of the five new methods, plus at least one
  end-to-end test through a real daemon (per
  `test_lifecycle_integration.py`'s pattern) proving a put on one
  `GrpcDaemon` instance is visible via `get_annotations` on a second,
  independently-connected `GrpcDaemon` instance — this is the
  multi-client scenario UC-28.S-1/S-3 describe, and ticket 005's
  integration ticket will lean on this same pattern being proven here
  first at the wire level.
- **Verification command**: `uv run pytest -m "not camera"` and
  `uv run mypy`.

**Documentation updates**: none beyond docstrings.
