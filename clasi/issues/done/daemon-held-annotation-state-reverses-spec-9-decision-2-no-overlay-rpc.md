---
status: done
---

# Daemon-held annotation state (reverses spec §9 decision #2 "No overlay RPC")

## Description

Amend the v2 design docs and interface skeletons so that annotation
*authoring* becomes a daemon service while *rendering* stays client-side:
**the daemon stores annotations; it never renders them.** Clients author
paths/markers/symbols via RPC in world coordinates on a playfield; every
fetched/streamed frame embeds the current annotation set (exactly as frames
embed the `TagFrame`), so any client can render a fully annotated view from
one frame.

Motivation: multi-service communication (one service draws, others see) and
stateless clients — no client-side files or directories, just a daemon
connection.

Stakeholder decisions (settled, 2026-07-31):

- **In-memory only** — annotations vanish on daemon restart, like the
  mobile-tag registry; clients re-draw on reconnect.
- **Named layers** — every annotation belongs to a client-chosen layer
  (e.g. `planner`, `robot-3`); clear/replace by layer or id; no ownership
  enforcement (last write wins); viewer panels toggle layers.

Framing sentence to reuse across the docs: *annotations are runtime state
like the mobile-tag registry (`Daemon.register_tag`), not configuration — so
record-level RPCs are consistent with the whole-document config rule.*

## Cause

The stakeholder reversed the 2026-07-29 resolved decision
(specification.md §9 #2, "No overlay RPC") that made drawing a pure
client-side library. That decision is woven through the design set —
spec §3/§4.5/§4.6/§4.7/§6/§8, design.md principle 6, adapters.md, the
use-case files, and the `Canvas` protocol in `client/interfaces.py` — so the
reversal must be recorded as a superseding dated decision and propagated
consistently, never silently rewritten.

## Proposed fix

### New vocabulary / API shape

**`src/aprilcam/types.py`** — frozen dataclasses:

- Shapes: `Marker(at: WorldPoint, label: Optional[str])`,
  `PathShape(points: tuple[WorldPoint, ...])`, `Polygon(points)`,
  `Circle(center: WorldPoint, radius_cm: float)`,
  `Text(at: WorldPoint, text: str)`. Shape union (valid by construction)
  rather than a kind-enum with optional fields — matches the codebase's
  "no confusable states" rationale (`TagRecord` None-vs-(0,0)).
- Envelope: `Annotation(id: str, layer: str, shape: <union>, style: Style,
  ttl_seconds: Optional[float] = None)`. Key is **(playfield, layer, id)**;
  id unique *within a layer*.
- `Style(color, line_width)` — geometry is world-cm always; style is
  presentation, may be pixel-denominated, renderers may ignore hints. Say so.
- `AnnotationSet(annotations: tuple[Annotation, ...], revision: int,
  timestamp: float)` — monotonic `revision` lets renderers skip
  re-rasterizing an unchanged overlay at 30 fps.
- `ImageFrame.annotations: Optional[AnnotationSet] = None`, parallel to
  `tags`, same documented three-way distinction (None = not carried /
  empty = nothing drawn).

**`src/aprilcam/daemon/interfaces.py`** — new `# -- annotations --` section
on `Daemon`, placed beside `register_tag` and citing the analogy explicitly:
`put_annotation(playfield, annotation)` (upsert),
`remove_annotation(playfield, layer, id)`,
`replace_layer(playfield, layer, annotations)` (atomic swap — the batch-put
path for a planner redrawing its whole layer in one RPC),
`clear_annotations(playfield, layer=None)`,
`get_annotations(playfield, layer=None) -> AnnotationSet`.
All **playfield-addressed** — the crisp rule: observations are
camera-addressed; field state (surveyed features, annotations) is
playfield-addressed; frame embedding is the join via the camera's playfield
link.

**`src/aprilcam/client/interfaces.py`** — `Canvas` is replaced by
**`Renderer`**: `render(frame: ImageFrame, *,
layers: Optional[tuple[str, ...]] = None,
extra: tuple[Annotation, ...] = ()) -> bytes` (PNG). Authoring moved to the
daemon; the renderer is the cv2-free rasterizer of embedded tags
(boundary + hat, absorbed from `Canvas.tag`) and annotations, using frame
metadata for world→pixel. `extra` preserves purely-local ad-hoc drawing with
the same `Annotation` types — no second vocabulary.

**MCP (spec §4.7)** — drawing tools return as thin daemon-RPC wrappers:
`draw_marker`, `draw_path`, `draw_polygon`, `draw_circle`, `draw_text`,
`remove_annotation`, `clear_layer`, `list_annotations`. World-coordinate,
no cv2; vision boundary intact.

### Open questions to RECORD (docstring/spec "Open:" style, not decide)

TTL semantics (reset on upsert? lazy pruning?); count/point limits and
behavior at the limit; embed policy (always vs non-empty); unknown playfield
on put (reject vs hold — `register_tag` analogy cuts the other way); **raw
frames** — lean "no annotations on raw" (world-only + no client-side
mapping; UC-16 "raw means raw"), record the rejected alternative; layer-name
collisions with viewer built-ins (`tags`, `features`); whether
`SetPlayfield` replacement clears that playfield's annotations; CLI
authoring surface (process.md CLI/Python parity requires at least
list/clear); no `stream_annotations` in v2 (frames are the publish channel)
— record as out-of-scope; ROS republishing (record in adapters.md §6).

### Ordered edits

1. **docs/design/specification.md** (first — §9 is the authority the rest
   cites)
   - §9: append to decision 2 "(Superseded 2026-07-31: see decision 8)";
     add **decision 8** (dated) with the full model + the two settled
     sub-decisions.
   - §3: rewrite "Drawing … is not a service at all" paragraph → authoring
     is a daemon service, rendering client-side; update `aprilcam view`
     table row.
   - §4.1: add annotation RPCs to the control-plane bullet; add a sibling
     "Frames carry annotations" bullet next to "Frames carry tags" (state
     the raw-frame decision there).
   - §4.5: keep "no workspace directory, no paths.json"; replace "no shared
     annotation state" with the daemon-held in-memory store.
   - §4.6: rewrite drawing-library section into authoring-RPC + client
     Renderer halves; delete the embed-your-own-MCP-server paragraph.
   - §4.7: drawing tools return (list above); frame-processing removal
     unchanged.
   - §4.8: viewer renders daemon annotation layers; panels toggle layers by
     name.
   - §6 removed-table "Paths as a service" row: keep the row (v1 mechanism
     stays removed), amend the "why" to note the 2026-07-31 return in a new
     shape.
   - §8: shared half happened; keep only *persistent* annotation state as
     future; note it layered on the runtime-state model, not the config
     service.
2. **docs/design/design.md** — principle 6 → "Annotations are daemon state;
   rendering is a client library"; principle 4 + one sentence distinguishing
   configuration documents from runtime registrations; §3 table (daemon
   gains annotation store; client drawing library → renderer); §5 wire
   contracts (annotation RPCs; frames embed TagFrame **and** annotation
   set); §9 gap item 4 reworded.
3. **docs/design/adapters.md** — §2: amend test (c) to "computed **or
   held** once and shared" and argue it (test (b) also passes: without the
   daemon, authors would need their own query servers = adapters talking to
   each other, forbidden by §5.3); §3 daemon ownership list; §4.4 renderer;
   §6 ROS open question.
4. **docs/design/configuration.md** — §5 file-backed/survives-restart
   table: add "Annotations | no | no" row.
5. **docs/design/use-cases/README.md** — add **JTBD-07** "Communicate
   spatial intent to everyone looking at the same field" (must precede
   UC-28 per the no-invented-vocabulary rule); update subsystem-files
   table.
6. **docs/design/use-cases/daemon.md** — new **UC-28 "Draw on the playfield
   for everyone to see"**: happy scenarios in concrete values (layer
   `planner`, marker `goal` at (50, 30) on `main-playfield`); failure
   scenarios (unknown playfield; clear of nonexistent layer; restart wipes
   — a documented happy-path variation; same (layer,id) upsert race →
   last-write-wins). Update traceability/coverage counts + decisions table.
7. **docs/design/use-cases/cli.md** — UC-16: add the sentence making "raw
   means raw" load-bearing for annotations; UC-18 open question updated
   (panels ↔ layers answered, defaults remain open); command surface:
   annotation list/clear commands, writes excluded from `--all` fan-out.
8. **docs/design/development-plan.md** — Stage 8 (store + embed + tests:
   round-trip, restart-wipes, embed-matches-store, upsert), Stage 10
   (renderer replaces drawing library; exit-criterion example: connect →
   put marker + path → fetch deskewed frame → render → PNG; note issue
   `replace-paths-service-with-client-drawing-library.md` superseded — cut
   a new issue, don't repurpose), Stage 11 (MCP drawing tools + fake-daemon
   tests), Stage 12 (viewer layer panels), issue-map row 10.
9. **src/aprilcam/types.py** — new types + `ImageFrame.annotations`
   (docstring parallel to "Frames carry tags", with Open items).
10. **src/aprilcam/daemon/interfaces.py** — annotation section per shape
    above, docstrings citing UC-28, in-memory rule, (layer,id) key,
    last-write-wins, Open items.
11. **src/aprilcam/client/interfaces.py** — `Canvas` → `Renderer` per shape
    above (import Annotation types).
12. **src/aprilcam/playfield/interfaces.py** — module-docstring prose only:
    playfield is the addressing scope for annotations (protocol unchanged).
13. **docs/design/design-audit.md** — one-line dated addendum to §3.9
    ("premise reversed by spec §9 #8; v1 mechanism stays removed").

### Flag to stakeholder (pre-existing, do not silently fix)

Spec §5 storage layout lists `data_dir/tags.json — mobile-tag registry`, but
`MountParameters` (types.py) and configuration.md §5 say mount params are
in-memory only. Since annotations are modeled on that registry, this
contradiction should be resolved in the same pass — surface it, ask which is
intended.

## Verification

- `uv run python -c "import aprilcam.types, aprilcam.daemon.interfaces,
  aprilcam.client.interfaces, aprilcam.playfield.interfaces"` — skeletons
  import cleanly; `dataclasses.fields` shows the new fields.
- `grep -rn "client drawing library\|§4.6\|drawing is a client-side\|not a
  service at all" docs/design/ src/` — no stragglers still asserting the
  old model except the honest supersession notes.
- Re-tally use-case coverage counts in use-cases/README.md and daemon.md.
- No test suite exists yet (skeleton phase).

## Related

- specification.md §9 decision #2 (2026-07-29, being superseded) and §8
  future item (anticipated this revisit).
- `ImageFrame.tags` / boundary-and-hat contract (added 2026-07-31) — the
  frame-embedding pattern annotations follow.
- Mobile-tag registry (`Daemon.register_tag`, UC-12) — the in-memory
  runtime-state precedent.
- Development-plan issue `replace-paths-service-with-client-drawing-library.md`
  — superseded in shape by this issue.
