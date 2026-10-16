# tags/

**Owner:** Eric Busboom · **Last reviewed:** 2026-10-16 · **Status:** stable

---

## 1. Purpose

`tags/` generates printable AprilTag (36h11) and ArUco (4x4) fiducials
(UC-21) — by default one labelled PDF sheet, one tag per page — nothing else. It is **purely offline**: no daemon, no camera, no
network, no import of `daemon/`, `client/`, or `vision/` anywhere in its call
path. It is the one capability in the whole system that works on a bare
install, which is exactly when it's needed — an operator generates tags
*before* there is a field to look at.

**Revised, sprint 036.** Sprint 028 originally recorded OpenCV as a
**permanent, unconditional base** dependency specifically so `aprilcam
taggen` would work on a bare install with zero setup — the whole point of
`tags/` being "the one capability that works before there is a field to
look at." Sprint 036 reversed that: `opencv-contrib-python` moved to the
`daemon` extra (closing an unrelated, accidental packaging gap that let
`cv2` leak into `client/renderer.py` and `cli/locate.py` — see
`calibration/DESIGN.md`), and this module's own `cv2` import became
**lazy, at the point of use** rather than module scope. The command is
still importable and discoverable everywhere (`tags/generator.py` itself
imports cleanly with no `cv2` in the process), and `design.md` §2's
single-vision-authority principle still names `tags/` as its one
import-boundary exception — but *running* `tags generate`/`aprilcam
taggen` without the `daemon` extra installed now fails with a clear
"install `aprilcam[daemon]`" message instead of silently working, because
`cv2` genuinely is not there to import. §4 and §6 below detail the
mechanism and the trade this reversal accepts. Being importable everywhere
is not a license to do pixel work outside the daemon, though: generation
only ever writes images, it never reads or processes them.

The two supported dictionaries are fixed system-wide (spec §4.10): AprilTag
**36h11** and ArUco **4x4** — a `TagFamily` value fully determines the
dictionary, so no dictionary parameter appears anywhere in this module's
surface.

## 2. Orientation

One behavioral interface, one concrete implementation, both named
`TagGenerator`:

- **`interfaces.py` — `TagGenerator`.** A plain base class (not
  `typing.Protocol` — CLAUDE.md's interfaces rule), with three methods, all
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
    This is the opt-in `--png` path.
  - `generate_sheet(family, ids, *, out_path) -> Path` — **the default
    deliverable** (sprint 043, ticket 002): one multi-page PDF, one tag per
    59 x 102 mm label page, tag centred with a small quiet zone and a
    caption beneath (`ArUco 4x4 ID: 3` / `AprilTag 36h11 ID: 7`). **No size
    parameter**: the tag fills the page, the operator prints at 100% and
    measures the result; size is an output of printing, not an input. No
    position text is ever drawn (v1's `ARUCO_POSITIONS` captions were stale
    and are deliberately gone).

- **`generator.py` — `TagGenerator`.** The concrete class, deriving from
  `tags.interfaces.TagGenerator` the same way `client.discovery.Discovery`
  derives from `client.interfaces.Discovery`. Nothing else
  from this tree beyond `errors`, `types`, and its own `interfaces` module —
  no `daemon`, `client`, or `vision` import anywhere in the call path,
  matching the offline-utility contract in §1. `cv2` is imported **lazily**
  (sprint 036) — inside the functions that actually need it, not at module
  scope — with `ImportError` caught and re-raised as a clear
  "install `aprilcam[daemon]`" message; simply `import
  aprilcam.tags.generator` or constructing a `TagGenerator` touches no
  `cv2` symbol at all.

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

0. **Import `cv2`, or fail clearly (sprint 036).** The first thing
   `generate()` (and `valid_ids()`, which it calls) does is import `cv2`
   inside the function body; an `ImportError` here is caught and
   re-raised as a message naming `pip install 'aprilcam[daemon]'` as the
   fix, never a bare traceback. The family→dictionary mapping
   (`_DICTIONARY_BY_FAMILY`, previously a module-level dict built from
   `cv.aruco.DICT_APRILTAG_36h11`/`DICT_4X4_50` at import time) moves
   inside this same lazy-import boundary so that importing this module —
   or constructing a `TagGenerator` — never touches a `cv2` symbol.
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

### Sheet (`generate_sheet`)

Same up-front order as `generate()`: import `cv2`, validate every id
(`InvalidTagId`), check `out_path` writability (`UnwritableOutputPath`,
also refusing a directory) — all before anything is rendered or written.
Then, per id: render the marker at 960 px (divisible by 6 and 8 so every
module is whole pixels for both families), pad a 0.04-of-edge white quiet
zone (v1's PDF value), encode to PNG in memory, and place it on a fresh
59 x 102 mm `fpdf2` page with 1 mm margins — `min(57, 94)` = 57 mm square,
horizontally centred, vertically centred in the area above a 6 mm caption
band — with a 7 pt Helvetica caption 1 mm below. Page geometry, quiet-zone
ratio and caption placement are v1's, re-derived fresh. `fpdf2` is a **base**
dependency (pure Python; moved out of the `daemon` extra in sprint 043),
imported where used.

`valid_ids()` itself is a one-liner: look up the family's OpenCV predefined
dictionary and return `range(0, dictionary.bytesList.shape[0])`.

## 5. Interfaces

### Exposes

- `tags.generator.TagGenerator` — `valid_ids(family: TagFamily) -> range`;
  `generate_sheet(family, ids, *, out_path) -> Path` (the PDF);
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
  from this codebase. Imported lazily (sprint 036), from the `daemon`
  extra — see §4, step 0.

The one caller today is `cli/tags.py`'s `cmd_generate` (`aprilcam tags
generate IDS [-A] [-o PATH] [--png [--size-cm CM] [--dpi DPI]]`, UC-21,
v1's argument shape). It is a thin argv-to-call shell: parses the ids spec
("1-8", "0-3,7") into a `tuple[int, ...]` (id *validity* is deliberately
not checked there — that whole-batch check belongs to the generator), then
calls `generate_sheet()` (default, PDF at `-o`, default `tags.pdf`) or, with
`--png`, `generate()` (images into directory `-o`, default `tags/`;
`--size-cm` optional, default 5 cm; `--dpi` default 300) exactly once with
the whole id tuple, letting `InvalidTagId`/`UnwritableOutputPath` flow
through `cli._common.report_error` unchanged. `--size-cm`/`--dpi` without
`--png` are refused: the PDF path takes no size. Unlike every other command
in `cli/tags.py`, `tags generate` takes no `-d`/`--daemon` or `--all` flag —
consistent with §1's offline contract.

## 6. Open Questions / Known Limitations

- **PDF output — resolved (sprint 043, ticket 002).** Previously open;
  the stakeholder decided (2026-10-16) that v1's printable labelled sheet is
  the right deliverable. `generate_sheet` is the default; per-id PNGs
  survive as `--png`. `fpdf2` moved to the base dependencies.
- **`docs/design/specification.md` §4.10's stale sentence is now partially
  re-aligned, not fully resolved.** It describes OpenCV as tolerated in
  `tags/` "or replaced with pre-rendered dictionaries to make the base
  install fully cv2-free." Sprint 028 recorded this as the opposite of
  the then-current decision (cv2 as a permanent, unconditional base
  dependency); sprint 036's reversal (lazy import, `daemon`-extra-gated)
  lands somewhere between the two — the base install is `cv2`-free at
  *import* time but `tags generate` still needs the `daemon` extra to
  actually run, which is neither "permanent base dependency" nor
  "pre-rendered dictionaries." `specification.md` §4.10 itself is still
  unedited; reconciling its exact wording remains standing
  architecture-consolidation work, not this sprint's ticket scope.
- **The lazy-import trade accepted by sprint 036, stated plainly.** A
  fresh base-only install can no longer generate a tag image without an
  extra install step (`pip install 'aprilcam[daemon]'`) — a real,
  if small, regression in the "just works" experience sprint 028
  deliberately optimized for. This was accepted because the alternative
  (keeping `opencv-contrib-python` an unconditional base dependency) is
  what let `cv2` leak into `client/renderer.py` and `cli/locate.py`
  (`calibration/DESIGN.md`'s resolved gap) and contradicts the
  higher-authority, stakeholder-facing constraint in the project overview
  ("OpenCV is installed only via the `daemon` extra; the base package
  requires Pillow instead") — that constraint names no `tags/` exception,
  unlike `design.md` §2's own architectural principle, which does. Given
  the conflict, the stakeholder-facing constraint took precedence.
- Both `tags/interfaces.py` and `tags/generator.py`'s own module docstrings
  (as originally authored) describe the concrete `TagGenerator` as not yet
  existing ("No concrete implementation exists yet (out of this pass's
  scope)"). That is stale relative to the current tree — ticket 028-001
  implemented and wired it up (`cli/tags.py`'s `cmd_generate` uses it in
  production), and the `interfaces.py` docstring in particular has not been
  updated to reflect that.
