---
id: '010'
title: Annotation store count/point limits
status: done
use-cases:
- SUC-010
depends-on: []
github-issue: ''
issue: hardening-cleanup-and-the-design-doc-set.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Annotation store count/point limits

## Description

`daemon/service.py`'s `put_annotation` and `replace_layer` — the two
mutation points that can grow the annotation store
(`remove_annotation`/`clear_annotations` only shrink it) — enforce no
bound on annotation count or per-shape point count. Sprint 024's own
recorded decision explicitly scoped limits out ("not enforced this
sprint. No cap on annotation..."). A misbehaving client can grow the
store, and the per-frame embedded payload, without limit — unbounded
memory and per-frame wire cost.

Add a per-layer annotation-count bound and a per-shape point-count
bound (for `PathShape.points`/`Polygon.points`, `types.py` — the two
shape types with unbounded point tuples; `Circle`/`Rect`/`Text` are
fixed-parametric and need no such bound). Reject an over-bound request
with a new typed error (`errors.py`, following the existing
`InvalidTagId`/`MalformedDocument` naming and metadata pattern) naming
the limit and the offending value, instead of silently accepting it.

**Fixed constants, not a configurable document field** (Design Rationale
in `sprint.md`): sprint 024 scoped limits out entirely, so even a fixed
bound is new ground; making it configurable (per-playfield or global,
settable via a pushed document) is a larger design surface this sprint
does not need to open.

## Acceptance Criteria

- [x] `put_annotation` rejects an addition that would exceed the
      per-layer annotation-count bound, raising a new typed error
      naming the bound and the current count — the store is left
      unchanged (no partial mutation) on rejection.
- [x] `replace_layer` rejects a replacement layer whose annotation count
      exceeds the bound (same error type) — the store is left with the
      layer's *previous* contents unchanged on rejection, matching this
      method's existing atomicity guarantee (UC-28.S-2: no consumer
      ever observes a half-updated layer).
- [x] Either mutation rejects a `PathShape`/`Polygon` whose `points`
      tuple exceeds the per-shape point bound, with the same typed
      error family naming the bound and the actual point count.
- [x] A request at or under both bounds is completely unaffected — no
      behavior change, no new error, for any request within limits
      (regression coverage against every existing annotation-store
      test).
- [x] The chosen bound values are specific numbers, proposed in this
      ticket's own implementation (per `sprint.md`'s Open Questions,
      which deliberately left the exact numbers to this ticket),
      grounded in a real playfield's plausible annotation load — a
      path tracing a robot's route around a real-sized playfield, for
      instance — generous enough that no known legitimate use is
      anywhere close to the bound.
- [x] `uv run mypy` clean.

## Implementation Plan

**Approach**: add the count/point checks at the top of
`put_annotation`/`replace_layer`, before any mutation of `self._annotations`
or `self._annotation_revisions` — a rejected request must have zero
side effects, matching this module's existing "no consumer ever
observes a half-updated layer" discipline. Pick specific bound
constants (module-level, e.g. `_MAX_ANNOTATIONS_PER_LAYER`,
`_MAX_POINTS_PER_SHAPE`) and justify the chosen numbers in a code
comment at their definition, referencing a plausible real-playfield
scenario (e.g. a path with waypoints every few centimeters around a
multi-meter playfield perimeter).

**Files**:
- `src/aprilcam/daemon/service.py` — `put_annotation`, `replace_layer`
  gain the bound checks.
- `src/aprilcam/errors.py` — new typed error (e.g.
  `AnnotationLimitExceeded`), following the existing error-class
  pattern (a plain class deriving from `AprilCamError`, carrying enough
  structured data — bound, actual value, which bound was exceeded — for
  `client/daemon_client.py`'s `_reconstruct_error`-style reconstruction
  if this error needs to cross the RPC boundary; check whether
  `daemon/rpc.py`'s `_abort` contract needs a new
  `aprilcam-error-type` case wired in for this error to surface
  correctly to RPC clients, not just local callers).
- `src/aprilcam/proto/aprilcam_v2.proto` / regenerated stubs — only if
  the RPC layer needs a new error-metadata case wired through (see
  above); no new RPC method or message field is anticipated.

**Testing plan**:
- **Existing tests to run**: `uv run pytest
  tests/aprilcam/daemon/test_annotations_integration.py
  tests/aprilcam/daemon/test_service.py -v` (confirm no regression to
  the existing annotation-store suite this ticket extends).
- **New tests to write**: `put_annotation` at the per-layer count bound
  succeeds, one over rejects with the typed error and no mutation;
  `replace_layer` with an over-bound layer rejects, leaving the
  previous layer contents intact; a `PathShape`/`Polygon` at the
  per-shape point bound succeeds, one point over rejects; a request
  well under both bounds is unaffected (regression).
- **Verification command**: `uv run pytest -m "not camera"` and
  `uv run mypy`.

**Documentation updates**: none required in `docs/design/` proper — the
`daemon/` subsystem `DESIGN.md` does not exist yet; ticket 008
documents this behavior when it bootstraps that file (ticket 008
depends on this ticket for exactly that reason).
