# tags/

**Owner:** Eric Busboom · **Last reviewed:** 2026-08-21 · **Status:** stable

---

## 1. Purpose

`tags/` generates printable AprilTag (36h11) and ArUco (4x4) fiducial images
(UC-21) — nothing else. It is **purely offline**: no daemon, no camera, no
network, no import of `daemon/`, `client/`, or `vision/` anywhere in its call
path. It is the one capability in the whole system that works on a bare
install, which is exactly when it's needed — an operator generates tags
*before* there is a field to look at.

This is why OpenCV is a **base** dependency rather than gated behind the
`daemon` extra the rest of the vision stack lives behind (design.md §2's
single-vision-authority principle names `tags/` generation as its one
carved-out exception). Being importable everywhere is not a license to do
pixel work outside the daemon, though: generation only ever writes images, it
never reads or processes them.

The two supported dictionaries are fixed system-wide (spec §4.10): AprilTag
**36h11** and ArUco **4x4** — a `TagFamily` value fully determines the
dictionary, so no dictionary parameter appears anywhere in this module's
surface.

## 2. Orientation

One behavioral interface, one concrete implementation, both named
`TagGenerator`:

- **`interfaces.py` — `TagGenerator`.** A plain base class (not
  `typing.Protocol` — CLAUDE.md's interfaces rule), with two methods, both
  `raise NotImplementedError` bodies:
  - `valid_ids(family) -> range` — the family's dictionary range, published
    so a caller (the CLI) can validate before generating and name the valid
    range in its error.
  - `generate(family, ids, *, size_cm, out_dir, dpi=300) -> tuple[Path,
    ...]` — writes one image per id, sized to print at `size_cm`. `size_cm`
    is the physical edge length the printed tag must have; it ties
    generation both to survey (corner ArUcos must match the playfield
    definition, UC-21.S-1) and to detection (a robot tag must match the
    `size` registered for mobile-tag mount parameters, UC-21.S-2, UC-12).

- **`generator.py` — `TagGenerator`.** The concrete class, deriving from
  `tags.interfaces.TagGenerator` the same way `client.discovery.Discovery`
  derives from `client.interfaces.Discovery`. Imports `cv2` and nothing else
  from this tree beyond `errors`, `types`, and its own `interfaces` module —
  no `daemon`, `client`, or `vision` import anywhere in the call path,
  matching the offline-utility contract in §1.

Note: `tags/__init__.py` is currently empty (no re-exports) — callers import
directly from `tags.generator` and `tags.interfaces`.

## 3. Constraints and Invariants

- **Whole-batch validation before any write; nothing partially lands on
  disk.** `generate()` checks every requested id against `valid_ids(family)`
  up front — a single bad id in the batch rejects the *whole* batch
  (`InvalidTagId`) before anything is rendered or written (UC-21.S-3,
  SUC-002). Output-directory writability is checked next, also before any
  rendering (`UnwritableOutputPath`, SUC-003). Only after both checks pass
  does rendering/writing begin.
- **The family→dictionary mapping is deliberately duplicated, not shared.**
  `vision/detector.py` already has an identical `_DICTIONARY_BY_FAMILY` near
  the top of its own module. `generator.py` does not import it:
  `vision/` is daemon-only detection machinery, and creating an import edge
  from `tags/` to it would couple two modules with no shared reason to
  change together (sprint 028's Design Rationale, Step 6 — "detection
  tuning" and "printable image generation" are different reasons to change).
  Importing it would also make `tags/`'s "no daemon needed" property merely
  behavioral instead of structural. The cost is that a reviewer changing one
  mapping (e.g. adding a new AprilTag family) must remember to check the
  other — mitigated only by a cross-reference comment in both files, not by
  shared code. This is a considered trade, not an oversight.
- **A size unachievable at the given DPI warns, never fails.** There is no
  single hard pixel-count floor below which a printed tag stops detecting —
  it depends on printer DPI, camera resolution, and viewing distance, none
  of which `generate()` knows. The chosen threshold (300px per edge,
  `_MIN_LEGIBLE_PIXELS`) is a conservative round number, comfortably above
  what a 300 DPI consumer printer renders for a 2.5cm tag and above the
  ~100px floor where OpenCV's own detectors start missing corners in
  practice. Below it, `generate()` emits a `warnings.warn` — it still
  generates the image, because a small preview or a close-range-only tag may
  be exactly what the operator wants. This exact numeric threshold is
  recorded as an implementation-level judgment call (sprint 028's Open
  Question 4), not a spec-mandated number — it's revisable.
- **Output-directory writability check mirrors, but does not import,
  `config/directories.py`'s writability helper.** `tags/` has no
  dependency on `config/` (or anywhere else) beyond `errors`/`types`/its own
  `interfaces`; the check (`_out_dir_writable`) is small enough that
  duplicating it costs less than adding an import edge.

## 4. Design

`TagGenerator.generate()`'s algorithm, in the order it actually runs
(`generator.py`):

1. **Validate every id up front.** `self.valid_ids(family)` computes the
   dictionary's range (`cv.aruco.getPredefinedDictionary(...).bytesList.shape[0]`,
   0-based); each requested id is checked against it, raising `InvalidTagId`
   on the first miss — before anything is rendered.
2. **Check output-directory writability before anything is rendered.**
   `_out_dir_writable(out_dir)` — true if the path exists and is
   writable/executable, or doesn't exist yet but its nearest existing
   ancestor is (so `generate` can create the directory on demand). A false
   result raises `UnwritableOutputPath`. Only after this check does
   `out_dir.mkdir(parents=True, exist_ok=True)` actually create it.
3. **Compute pixel size from the physical target; warn, don't fail, below
   the legibility threshold.** `pixel_size = round(size_cm / 2.54 * dpi)`
   (cm → inches → pixels at the requested DPI); if it falls below
   `_MIN_LEGIBLE_PIXELS` (300), `warnings.warn(...)` fires but generation
   continues.
4. **Render and write one PNG per id.** For each id:
   `cv.aruco.generateImageMarker(dictionary, tag_id, pixel_size)` renders the
   marker image, written to `out_dir / f"{family.value}-{tag_id}.png"` via
   `cv.imwrite`. The full tuple of written paths is returned.

`valid_ids()` itself is a one-liner: look up the family's OpenCV predefined
dictionary and return `range(0, dictionary.bytesList.shape[0])`.

## 5. Interfaces

### Exposes

- `tags.generator.TagGenerator` — `valid_ids(family: TagFamily) -> range`;
  `generate(family, ids, *, size_cm, out_dir, dpi=300) -> tuple[Path, ...]`.
- `tags.interfaces.TagGenerator` — the base class it derives from.
- Typed errors (`aprilcam.errors`): `InvalidTagId` (names the valid range;
  nothing generated or written), `UnwritableOutputPath` (raised before any
  image is rendered or encoded).

### Consumes

- `aprilcam.types.TagFamily` — the enum this module's whole surface is keyed
  on (`str` subclass, so `TagFamily(args.family)` round-trips cleanly from a
  CLI `choices=` string).
- `aprilcam.errors` — `InvalidTagId`, `UnwritableOutputPath`.
- `cv2` (`cv.aruco`) — the only external vision dependency; nothing else
  from this codebase.

The one caller today is `cli/tags.py`'s `cmd_generate` (`aprilcam tags
generate --family FAMILY --ids SPEC --size-cm CM --out DIR [--dpi DPI]`,
UC-21). It is a thin argv-to-call shell: parses the `--ids` spec
("1-10", "1-3,55") into a `tuple[int, ...]` (id *validity* is deliberately
not checked there — that whole-batch check belongs to `TagGenerator.generate`
itself), then calls `generate()` exactly once with the whole id tuple (never
looping per-id), letting `InvalidTagId`/`UnwritableOutputPath` flow through
`cli._common.report_error` unchanged. Unlike every other command in
`cli/tags.py` (`mount register/clear/list`, which are daemon RPCs), `tags
generate` takes no `-d`/`--daemon` or `--all` flag — there is no daemon in
its path for those flags to target, consistent with §1's offline contract.

## 6. Open Questions / Known Limitations

- **PDF / multi-tag-sheet output remains genuinely open** — flagged directly
  in the code ("Open (UC-21): PDF output for multi-tag sheets, as v1 had").
  Sprint 028 considered and explicitly declined it for that sprint: porting
  v1's `fpdf`-based sheet generator would move a second dependency
  (`fpdf2`, currently gated behind the `daemon` extra) into the base install
  for a capability none of that sprint's verification criteria needed, plus
  real page-layout logic (margins, per-page centering) beyond UC-21's core
  contracts. This was recorded as **not decided against** — genuinely open,
  a future sprint's call. Today an operator needing a multi-tag print sheet
  runs `tags generate` once per id/range and assembles a sheet externally.
- **`docs/design/specification.md` §4.10 has a stale sentence.** It still
  describes OpenCV as tolerated in `tags/` "or replaced with pre-rendered
  dictionaries to make the base install fully cv2-free" — the opposite of
  UC-21's actual, recorded decision (cv2 as a permanent base dependency).
  Sprint 028 noted this explicitly as pre-existing drift it did not
  introduce and did not fix, since `specification.md`/`design.md` upkeep
  belongs to the standing architecture-consolidation process, not an
  individual sprint. Worth fixing opportunistically, not urgent.
- Both `tags/interfaces.py` and `tags/generator.py`'s own module docstrings
  (as originally authored) describe the concrete `TagGenerator` as not yet
  existing ("No concrete implementation exists yet (out of this pass's
  scope)"). That is stale relative to the current tree — ticket 028-001
  implemented and wired it up (`cli/tags.py`'s `cmd_generate` uses it in
  production), and the `interfaces.py` docstring in particular has not been
  updated to reflect that.
