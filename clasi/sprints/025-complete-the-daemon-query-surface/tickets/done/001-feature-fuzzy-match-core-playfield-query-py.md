---
id: '001'
title: Feature fuzzy-match core (playfield/query.py)
status: done
use-cases:
- SUC-001
depends-on: []
github-issue: ''
issue: complete-the-daemon-query-surface.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Feature fuzzy-match core (playfield/query.py)

## Description

Implement the first concrete `FeatureQuery` (`playfield/interfaces.py`),
in a new `playfield/query.py` module — the pure-logic foundation for
UC-08 ("where is the blue dot"). Ticket 002 wires this into the
daemon/RPC/CLI; this ticket is testable entirely standalone.

Mine `archive/src/aprilcam/core/playfield_query.py` for the matching
approach (tokenization, stopwords, type/cardinal synonyms, the bare-
"tag"-means-AprilTag tie-break) — adapt, don't port: v1 operated on raw
dicts read straight from `playfield.json`'s categories (`april_tags`/
`aruco_tags`/`rectangles`/`dots`); v2 operates on the already-loaded
`tuple[Feature, ...]` from `config.interfaces.PlayfieldDefinition.
features()` (`config/loader.py`'s concrete implementation), where each
`Feature` carries `slug`, `kind` (`april_tag`/`aruco_tag`/`rectangle`/
`dot` — the same string values v1 used), `position` (`WorldPoint`), and
optional `colour`.

**Cardinal matching works via slug tokenization, not a `cardinal`
field.** v2's `Feature`/`PlayfieldFeatureEntry` dataclasses carry no
separate `cardinal` field — `config/loader.py`'s own docstring states
this is deliberate (fields the on-disk schema doesn't model are
dropped by the loader), and this ticket does not reopen that decision.
`main-playfield`'s slugs already encode cardinal direction and colour
directly (`dot-west-blue`, `dot-northeast-orange`,
`dot-northwest-orange`, `dot-southeast-orange`,
`dot-southwest-orange`), so tokenizing the slug (split on `-`)
recovers cardinal matching without any schema change. Record this as
an explicit limitation in the module's docstring: a playfield whose
own slug convention doesn't encode cardinal direction will not support
cardinal queries until `Feature` gains a field for it — a real,
non-blocking gap this sprint's architecture review already flagged.

**Token sources per feature**: `kind` (split on `_` — `april_tag` ->
`april`, `tag`; plus type synonyms: `aruco_tag` -> `aruco`, `arucotag`,
`tag`; `rectangle` -> `rect`, `square`, `box`; `dot` -> `circle`,
`spot`, `disc`, `disk`), `colour` (lowercased), and `slug` (split on
`-`). v2's `Feature` carries no numeric id at all (unlike v1's
per-record `id`) — numeric "tag number one" style queries are
naturally out of scope for `find_feature`; that is `get_tag`'s job
(UC-09), already built.

**Matching policy — exact token-superset, no ranking, no threshold.**
A feature matches iff every one of the query's normalized tokens is
present in that feature's token set. This directly satisfies
UC-08.S-2 ("all four come back, never an arbitrary pick") and resolves
UC-08's own open question ("ranked-best-match or all-matches-above-
threshold?") in favor of neither — a deliberate simplicity choice, not
an oversight.

**Tie-break**: when the query says "tag" without "aruco"/"arucotag"
and both an `april_tag` and an `aruco_tag` match, keep only the
`april_tag` result(s) — mirrors UC-09.S-3's family-disambiguation
spirit (`main-playfield` holds both AprilTag 1 at the centre and ArUco
1 at a corner; a bare "tag" query must not silently blend them).

No match returns an empty tuple (UC-08.S-3) — `FeatureQuery.find`'s
contract is already `tuple[Feature, ...]`, so "not found" needs no
separate status or exception at this layer.

## Acceptance Criteria

- [x] `FeatureQuery` concrete implementation in `playfield/query.py`,
      deriving from `playfield.interfaces.FeatureQuery`.
- [x] UC-08.S-1: querying "the blue dot", "blue dot west", and the
      exact slug `dot-west-blue` against a `main-playfield`-shaped
      fixture all return exactly `dot-west-blue` at (-50, 0).
- [x] UC-08.S-2: querying "the orange dot" returns all four orange
      dots (`dot-northwest-orange`, `dot-northeast-orange`,
      `dot-southeast-orange`, `dot-southwest-orange`), never a single
      arbitrary pick.
- [x] UC-08.S-3: querying "the orange triangle" (no triangles exist)
      returns an empty tuple, never the nearest orange feature.
- [x] A bare "tag" query matching both an `april_tag` and an
      `aruco_tag` at different positions returns only the `april_tag`
      match(es).
- [x] The exact slug of any feature always matches that feature.
- [x] Module docstring records the cardinal-via-slug-tokenization
      limitation explicitly (no schema change to `Feature`/
      `PlayfieldFeatureEntry` in this ticket).
- [x] `uv run mypy` clean; no `typing.Protocol` in the diff —
      `FeatureQuery` derives from the existing plain base class.

## Implementation Plan

**Approach**: pure-function/pure-class module, no daemon, no RPC, no
CLI — testable by constructing a `PlayfieldDefinition`-shaped fixture
directly and calling `.find(query)`. This isolates the matching logic
from ticket 002's wiring, so that ticket can focus purely on daemon/
RPC/CLI plumbing against an already-tested core.

**Files**: `src/aprilcam/playfield/query.py` (new).

**Testing plan**:
- **Existing tests to run**: `uv run pytest
  tests/aprilcam/playfield/test_playfield.py -v` (confirm no
  regression to the existing `Playfield` dataclass this module sits
  beside), plus the full headless suite (`uv run pytest -m "not
  camera"`, baseline 795 headless tests passing).
- **New tests to write**: `tests/aprilcam/playfield/test_query.py` —
  a fixture playfield definition matching `main-playfield`'s worked
  example (AprilTag 1 at the centre, ArUco 1 at a distinct corner
  position, `dot-west-blue`, the four orange dots, at minimum),
  exercising every acceptance criterion above as a separate test case.
- **Verification command**: `uv run pytest -m "not camera"` and
  `uv run mypy`.

**Documentation updates**: none beyond the module's own docstring —
the design is already recorded in `sprint.md`'s Architecture section
and UC-08.
