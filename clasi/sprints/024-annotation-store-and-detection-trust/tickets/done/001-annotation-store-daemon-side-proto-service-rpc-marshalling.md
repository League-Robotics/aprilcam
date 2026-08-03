---
id: '001'
title: "Annotation store \u2014 daemon side (proto, service, RPC marshalling)"
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
depends-on: []
github-issue: ''
issue: annotation-store-for-shared-spatial-state.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Annotation store — daemon side (proto, service, RPC marshalling)

## Description

Build the daemon's half of the annotation store (spec §9 decision 8,
UC-28): the in-memory store inside `DaemonService`, the five gRPC RPCs
that expose it, and the deskewed-frame embedding. This is the
foundation ticket for the whole annotation-store story — tickets 002
(client wire), 003 (renderer), 004 (CLI), and 005 (integration) all
build on what this ticket ships.

**The vocabulary already exists — do not touch it.** `types.py`'s
`Annotation`, `AnnotationSet`, the shape union (`Marker`/`PathShape`/
`Polygon`/`Circle`/`Text`), `Style`, and `ImageFrame.annotations`
shipped in a prior architecture pass. This ticket implements *against*
them; it does not modify `types.py`.

**Follow the established vertical-slice pattern, don't invent a new
one.** This codebase has built exactly this shape of thing three times
already: sprint 020 tickets 004 (server: service + rpc) / 005 (client:
daemon_client + models) split a brand-new RPC surface along the
server/client seam; sprint 022 ticket 001 bundled a new multi-RPC
config-service slice (`SetPlayfield`/`GetPlayfield`/`ListPlayfields`/
`SetCameraConfig`/`GetCameraConfig`) as one server-side foundation
ticket. This ticket mirrors that split: server side only (proto +
service + rpc). No archive reuse — like ticket 022-001's playfield
work, this is new v2 code written fresh against an already-declared
interface, not a port of any v1 mechanism (v1's paths-as-a-service
mechanism is explicitly and permanently retired — see spec §6's
removed-table row and `docs/design/design.md` principle 6).

**Reuse `UnknownPlayfield` — do not add a new error type.**
`errors.py` already has it (used today by `get_playfield`); this
ticket's unknown-playfield handling raises exactly that, on every one
of the five new methods (resolution 2 in Architecture below — this is
broader than just `put`).

Builds:

- **`proto/aprilcam_v2.proto`** — additive messages `Annotation`,
  `AnnotationSet`, `Marker`, `PathShape`, `Polygon`, `Circle`, `Text`,
  `Style`, plus five RPCs (`PutAnnotation`, `RemoveAnnotation`,
  `ReplaceLayer`, `ClearAnnotations`, `GetAnnotations`) with their
  request/response messages, matching every existing message's
  Optional-field-via-`optional` discipline (`design.md` §6). Extend
  `GetFrameResponse` with the `annotations` field the header comment
  already reserves for this ("a later stage adds it here,
  additively"). Regenerate stubs via `scripts/compile_proto.py` — do
  not hand-edit the generated `_pb2.py`/`_pb2_grpc.py` files.
- **`daemon/service.py`** — `DaemonService` gains
  `self._annotations: Dict[str, Dict[str, Dict[str, Annotation]]]`
  (playfield → layer → id) and `self._annotation_revisions: Dict[str,
  int]` (playfield → monotonic revision), both guarded by the existing
  `self._lock`, alongside `_playfields`/`_camera_configs`/
  `_calibrations`. Five new methods implementing
  `Daemon.put_annotation`/`.remove_annotation`/`.replace_layer`/
  `.clear_annotations`/`.get_annotations`:
  - `put_annotation(playfield, annotation)` — `UnknownPlayfield` if
    the playfield isn't held; otherwise upsert by `(layer, id)`,
    increment that playfield's revision.
  - `remove_annotation(playfield, layer, id)` — `UnknownPlayfield` if
    unknown; a no-op (not an error) if the `(layer, id)` doesn't
    exist. Increments revision only on an actual removal.
  - `replace_layer(playfield, layer, annotations)` — `UnknownPlayfield`
    if unknown; a single dict-assignment swap of that layer's contents
    (`self._annotations[playfield][layer] = {a.id: a for a in
    annotations}`), atomic under `self._lock` — no consumer ever
    observes a half-updated layer. Increments revision.
  - `clear_annotations(playfield, layer=None)` — `UnknownPlayfield` if
    unknown; clearing a nonexistent layer (or an already-empty
    playfield) is a no-op, not an error. `layer=None` clears every
    layer on that playfield. Increments revision only if something
    was actually cleared.
  - `get_annotations(playfield, layer=None)` — `UnknownPlayfield` if
    unknown (this read validates the playfield exists exactly like
    `get_playfield` does — see Architecture resolution 2 for why this
    differs from the layer-level no-op rule one level down); returns
    an `AnnotationSet` with the current revision. `layer=None` returns
    every layer's annotations; a named layer filters to just that one
    (revision is still the whole-playfield revision — see sprint.md's
    Design Rationale for why).
  - `_get_deskewed_frame` grows one more overlay call:
    `annotations=self.get_annotations(calibration.playfield)`, always
    set (never `None`) on the deskewed branch — resolution 1 below.
    The **raw branch of `get_frame` is untouched** — never sets
    `annotations`, by contract (UC-16 "raw means raw").
- **`daemon/rpc.py`** — five new `_Servicer` methods
  (`PutAnnotation`/`RemoveAnnotation`/`ReplaceLayer`/
  `ClearAnnotations`/`GetAnnotations`), each following the existing
  try/except/`_abort` pattern exactly (no new business logic here —
  this module boundary is marshalling only, per its own docstring).
  `to_proto` converters for `Annotation`/`AnnotationSet`/each shape/
  `Style`, mirroring `_tag_record_to_proto`'s presence-checked-optional
  style field for field. `_image_frame_to_proto` grows a presence-set
  `annotations` field (present iff `frame.annotations is not None`,
  matching the existing `tags` field's own presence discipline one
  line above it).
- **`errors.py`** — no change. Reuses `UnknownPlayfield`.

**Resolved open questions this ticket implements directly** (see
sprint.md Architecture for the full reasoning — restated here as
concrete behaviour, not left for the implementer to re-derive):

1. **Embed policy**: a deskewed frame from a camera linked to a known
   playfield always carries a real `AnnotationSet` (never `None`),
   even when empty (revision 0). Only raw frames carry `None`.
2. **Unknown playfield**: `UnknownPlayfield` on **all five** methods,
   not just `put` — matching `get_playfield`'s existing precedent.
   Within a *known* playfield, an unknown *layer* is a no-op
   (`remove`/`clear`) or an empty result (`get`), never an error.
3. **`SetPlayfield` does not clear annotations** — no change needed
   here (this ticket doesn't touch `set_playfield`), but confirm by
   test that pushing a playfield definition of the same name leaves
   `self._annotations[name]` untouched.
4. **TTL** — `Annotation.ttl_seconds` round-trips through the store
   and the wire but drives no expiry behaviour. Do not implement
   pruning; this is a deliberate, recorded deferral, not an oversight
   to "helpfully" fix.
5. **Limits** — no cap on annotation count or path points. Do not add
   one; accepted for this sprint (local, single-operator system, no
   adversarial clients — stakeholder-confirmed).

## Acceptance Criteria

- [x] `put_annotation`/`remove_annotation`/`replace_layer`/
      `clear_annotations`/`get_annotations` implemented on
      `DaemonService`, all five raising `UnknownPlayfield` (naming the
      playfields held) for a playfield the daemon does not hold
      (UC-28.S-5).
- [x] `replace_layer` is atomic: a concurrent reader never observes a
      state that is neither the old set nor the new one (UC-28.S-2).
- [x] Two writers upserting the same `(layer, id)` — last write wins,
      no error, no merge (UC-28.S-6).
- [x] Clearing a layer that does not exist on a *known* playfield is a
      no-op, not an error.
- [x] A deskewed `GetFrame` embeds the linked playfield's current
      `AnnotationSet`, including when it is empty (revision 0, empty
      tuple) — never `None` on this branch.
- [x] A raw `GetFrame` never sets `annotations` (stays `None`),
      unconditionally — verified by a test that puts an annotation
      first, then fetches a raw frame from the same camera.
  - [x] `revision` increments on every actual mutation (put/remove/
      replace/clear-that-removed-something) and does **not** increment
      on a no-op (clear of an already-empty layer, remove of an absent
      key).
- [x] Pushing a `PlayfieldDefinition` of an already-held name via
      `set_playfield` leaves that playfield's stored annotations
      untouched (resolution 3).
- [x] `Annotation.ttl_seconds` round-trips through `put`→`get`
      unchanged; no pruning logic exists anywhere in this ticket's
      diff.
- [x] `uv run mypy` clean (`tests/test_typing.py` enforces this) — no
      `typing.Protocol` anywhere in the diff; `DaemonService` continues
      deriving from the plain-base-class `Daemon` (see
      `daemon/interfaces.py`'s module docstring — do not introduce
      `abc.ABC`).
- [x] Full suite green: `uv run pytest -m "not camera"` — baseline is
      717 headless tests passing today; this ticket only adds tests,
      never removes or skips an existing one.

## Implementation Plan

**Approach**: proto first (the wire contract every other file marshals
against), then `DaemonService`'s store methods (pure Python, testable
without gRPC at all — construct a `DaemonService` directly and call
the methods, exactly like `tests/aprilcam/daemon/test_service.py`
already does for `set_playfield`/`get_playfield`), then `daemon/rpc.py`
's `_Servicer` methods and converters last (these need the proto
messages to exist, and are the thinnest layer — mostly copy the
established `_tag_record_to_proto`/`GetTag` shape).

**Files**:
- `proto/aprilcam_v2.proto` (extend)
- `src/aprilcam/proto/aprilcam_v2_pb2.py`,
  `aprilcam_v2_pb2_grpc.py` (regenerated, not hand-edited)
- `src/aprilcam/daemon/service.py` (extend `DaemonService`)
- `src/aprilcam/daemon/rpc.py` (extend `_Servicer`, add converters)

**Testing plan**:
- **Existing tests to run**: `uv run pytest tests/aprilcam/daemon/
  test_service.py tests/aprilcam/daemon/test_lifecycle_integration.py
  -v` (both exercise `DaemonService`/the gRPC surface directly; neither
  should need a code change, only confirm no regression) plus the full
  headless suite (`uv run pytest -m "not camera"`).
- **New tests to write**: unit tests against `DaemonService` directly
  for every acceptance criterion above (no gRPC needed for these —
  mirror `test_service.py`'s existing construction pattern), plus at
  least one gRPC-level test (real channel, `daemon.__main__.build`-style
  fixture per `test_lifecycle_integration.py`'s pattern) proving the
  five RPCs round-trip through the wire correctly, including the
  `UnknownPlayfield` trailing-metadata reconstruction (mirrors the
  existing `_abort`/`_reconstruct_error` contract other RPCs already
  exercise).
- **Verification command**: `uv run pytest -m "not camera"` and
  `uv run mypy`.

**Documentation updates**: none required beyond the docstrings named
above (the design itself is already fully documented in sprint.md and
UC-28 — this ticket implements against existing documentation, it does
not need to write new design prose).
