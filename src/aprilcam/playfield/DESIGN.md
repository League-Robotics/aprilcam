# playfield/

**Owner:** Eric Busboom · **Last reviewed:** 2026-08-21 · **Status:** stable

---

## 1. Purpose

`playfield/` owns two things: the `Playfield` value type and the `FeatureQuery`
lookup behavior. Together they answer "what does the daemon know about this
field, and where is the blue dot on it" — without touching a camera.

A playfield is **not** a camera and **not** a service. It is a definition
document the daemon holds (the surveyed coordinate system plus ground truth of
what is physically on the field — tags, rectangles, dots — and where), plus
the queries that can be answered against that document alone. Cardinality
follows design.md §4: a camera observes exactly one playfield; a playfield has
zero or more cameras.

The playfield is also the **addressing scope for annotations** (UC-28):
drawings live on the field, not on any one camera's view of it. The
annotation RPCs themselves live on `Daemon` (design.md's "Annotations are
daemon state" principle, §2.6) — `playfield/` stays a definition document plus
queries, it does not grow annotation storage of its own.

This module has no dependency on `vision/`, `calibration/`, or any camera
code — see §5 for exactly what it does depend on.

## 2. Orientation

Two classes, one file each:

- **`playfield.py` — `Playfield`.** A frozen `@dataclass(kw_only=True)`
  deriving from `playfield.interfaces.Playfield`. Fields: `name`,
  `definition` (a `config.interfaces.PlayfieldDefinition`), and
  `camera_configs` (a snapshot tuple of every `CameraConfig` the daemon held
  at construction time — see §4). `linked_cameras()` filters that snapshot to
  the configs naming this playfield, in snapshot order.

- **`query.py` — `FeatureQuery`.** A frozen `@dataclass(kw_only=True)`
  deriving from `playfield.interfaces.FeatureQuery`, holding one field:
  `features: tuple[Feature, ...]`. Its `find(query)` method answers UC-08
  ("where is the blue dot") by reducing every surveyed `Feature` to a bag of
  lowercase keyword tokens and reducing the query string to the same shape,
  then matching by **exact token-superset, no ranking, no threshold**: a
  feature matches iff every one of the query's tokens is present in that
  feature's token set.

  Token sources per feature: `kind` split on `_` (`april_tag` → `april`,
  `tag`) plus a small synonym table (`aruco_tag` → also `aruco`, `arucotag`;
  `rectangle` → `rect`, `square`, `box`; `dot` → `circle`, `spot`, `disc`,
  `disk`); `colour` lowercased as one token when present; `slug` split on any
  non-alphanumeric run (`dot-west-blue` → `dot`, `west`, `blue`). Stopwords
  ("the", "a", "of", ...) are dropped from the query before matching so "the
  blue dot" isn't defeated by "the" matching nothing.

  A deliberate tie-break: a bare "tag" (no "aruco"/"arucotag" in the query)
  that matches both an `april_tag` and an `aruco_tag` keeps only the
  `april_tag` match(es) — mirroring UC-09.S-3's family-disambiguation
  convention that a bare "tag" means the AprilTag, not the ArUco marker.
  Saying "aruco" needs no special case: an `april_tag`'s tokens never contain
  "aruco", so the ordinary superset rule already excludes it.

Both classes are constructed fresh per call from already-loaded data —
`FeatureQuery(features=definition.features())` — never persisted, never
cached.

## 3. Constraints and Invariants

- **Answers come from the surveyed definition, not from vision.** `find`
  needs no calibration and no camera — but that is a real limitation, not a
  feature to gloss over: if someone physically removes the blue dot, this
  still reports where it is *supposed* to be. The definition is ground
  truth-as-declared, not ground truth-as-observed. (Colour *detection* is out
  of scope for v2 entirely — a coloured feature's position is what the
  definition says it is, never vision-confirmed.)
- **Ambiguity returns every match, never an arbitrary pick** (UC-08.S-2):
  "the orange dot" matching four features on `main-playfield` returns all
  four. Silently choosing one would be a wrong answer that looks right.
- **No match returns empty**, never a nearest-neighbor guess (UC-08.S-3):
  "the orange triangle" must not return the nearest orange thing as though it
  were a triangle. An empty query-token set (all stopwords) also returns
  empty rather than trivially matching everything.
- **`linked_cameras()` is derived at serve time, never stored.** A camera's
  config names its playfield; the link is read back from configs rather than
  duplicated in the playfield definition, so the two can never disagree.
- **Numeric queries are out of scope by design.** `Feature` carries no
  numeric id (unlike v1's per-record `id`), so "tag number one" style
  queries cannot resolve here — that's `get_tag`/`get_tags`'s job (UC-09),
  already built elsewhere. A token like "one" simply matches no feature's
  tokens rather than resolving to a wrong tag.
- **Cardinal matching is slug-tokenization only, not a real field** — a
  recorded, open, non-blocking limitation; see §6.

## 4. Design

**`Playfield.linked_cameras()`** (`playfield.py`): the interface docstring
mandates deriving the link at serve time from the daemon's held camera
configs, never storing it in the definition. The concrete class implements
this by carrying one field beyond the interface's `name`/`definition`: a
`camera_configs: Tuple[CameraConfig, ...]` snapshot. `linked_cameras()` is
then a pure filter — `tuple(c.name for c in self.camera_configs if
c.playfield == self.name)` — over that snapshot, in the snapshot's own order.

"Serve time" is enforced by construction discipline in the caller, not by any
mechanism inside `Playfield` itself: `daemon/service.py`'s `get_playfield`
and `list_playfields` both build a **fresh** `Playfield` on every call,
reading `self._camera_configs.values()` under the daemon's lock at call time.
`list_playfields` takes one camera-config snapshot and reuses it for every
playfield instance in that batch, so every entry in one response reflects the
same consistent moment even if configs change between output entries. There
is no caching and no background recomputation anywhere in this path.

**`FeatureQuery.find()`** (`query.py`): pure in-memory logic, no I/O. Given
the query string, token extraction lowercases, splits on non-alphanumeric
runs, drops stopwords, and dedups while preserving order. Given each
candidate `Feature`, a matching helper builds its token set (kind parts +
synonyms + colour + tokenized slug). `find` then does a plain set-superset
filter, applies the april/aruco tie-break described in §2, and returns the
surviving tuple verbatim — no sorting, no scoring.

`daemon/service.py`'s `find_feature(playfield, query)` is the one caller: it
looks up the named `PlayfieldDefinition` under lock, raises
`UnknownPlayfield` if it doesn't recognize the name, and otherwise builds
`FeatureQuery(features=definition.features()).find(query)` fresh — cheap
enough (just a tuple of already-materialized `Feature`s) that there is
nothing worth caching and no staleness to reason about, matching the same
"derived at serve time" discipline `Playfield.linked_cameras()` uses.

## 5. Interfaces

### Exposes

- `playfield.playfield.Playfield` (frozen dataclass: `name`, `definition`,
  `camera_configs`) — `linked_cameras() -> tuple[str, ...]`.
- `playfield.query.FeatureQuery` (frozen dataclass: `features`) —
  `find(query: str) -> tuple[Feature, ...]`.
- `playfield.interfaces.Playfield` / `playfield.interfaces.FeatureQuery` —
  the base classes both concrete types derive from (behavioral method
  `NotImplementedError` for `linked_cameras`/`find`; the value fields
  `name`/`definition` live on the interface's frozen dataclass too).

### Consumes

- `aprilcam.config.interfaces.Feature` and `PlayfieldDefinition` — the
  surveyed-data types this module reads (`definition.features()`) but never
  constructs or mutates.
- `aprilcam.config.interfaces.CameraConfig` — the snapshot type
  `Playfield.camera_configs` holds; `playfield/` never mutates or persists
  these, only filters by `.playfield`/`.name`.

Callers, for orientation (not a dependency of this module):

- `daemon/service.py` is the sole constructor of both types in production —
  `get_playfield`, `list_playfields`, and `find_feature` (see §4). It also
  raises `errors.UnknownPlayfield` around `find_feature`'s lookup, a
  responsibility that stays in `daemon/service.py`, not here.
- `mcp/tools/perception.py`'s `where` MCP tool and
  `client/daemon_client.py`'s `find_feature` wrapper both sit downstream of
  `Daemon.find_feature`, the RPC face of `FeatureQuery.find` — `playfield/`
  itself has no RPC or MCP surface of its own.

## 6. Open Questions / Known Limitations

- **Cardinal matching is slug-tokenization only, not a real field.**
  `Feature` (and the loader's entry type) deliberately carry no separate
  `cardinal` field. Cardinal queries like "blue dot west" work today purely
  because `main-playfield`'s slugs happen to encode cardinal direction as a
  token (`dot-west-blue`, `dot-northeast-orange`, ...). A future playfield
  whose slug convention doesn't encode cardinal direction would silently
  lose cardinal-query support — no fallback, no error, since `FeatureQuery`
  has no independent notion of "west" beyond whatever token happens to
  appear in a slug. This was flagged in sprint 025's architecture review as
  a recorded, open, non-blocking gap.
- Both concrete classes were, as of the `playfield/interfaces.py`/`query.py`
  authorship pass, described in their own module docstrings as "no concrete
  implementation yet" — that is stale relative to the current tree:
  `playfield.playfield.Playfield` (ticket 022-001) and
  `playfield.query.FeatureQuery` (ticket 025-001) are both implemented and
  wired into `daemon/service.py` in production use today. The
  `playfield/interfaces.py` module docstring itself has not been updated to
  reflect this.
