---
id: '001'
title: Implement TagGenerator and move OpenCV to base dependencies
status: open
use-cases: [SUC-001, SUC-002, SUC-003]
depends-on: []
github-issue: ''
issue: tag-generation-and-daemon-discovery.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Implement TagGenerator and move OpenCV to base dependencies

**Depends on**: — (foundation ticket, no dependency)
**Serves**: SUC-001, SUC-002, SUC-003 (UC-21)

## Description

`src/aprilcam/tags/` currently has only `interfaces.py` — `TagGenerator.
valid_ids`/`generate` both `raise NotImplementedError`. Implement a
concrete `aprilcam.tags.generator.TagGenerator` deriving from
`tags.interfaces.TagGenerator` (a plain base class, not
`typing.Protocol` — see `src/aprilcam/daemon/interfaces.py`'s module
docstring for the rule this whole codebase follows; `TagGenerator` is
purely behavioral, so `generator.py`'s class derives from it exactly
the way `client.discovery.Discovery` derives from `client.interfaces.
Discovery`).

`tags/` is documented (design.md §4.10, and the interface's own
docstring) as a standalone, purely-offline utility: no daemon, camera,
network, or `vision/`/`client/`/`daemon/` import anywhere in the call
path. `vision/detector.py` already has an identical family→dictionary
mapping (`TagFamily.APRILTAG → cv.aruco.DICT_APRILTAG_36h11`,
`TagFamily.ARUCO → cv.aruco.DICT_4X4_50`, `src/aprilcam/vision/
detector.py:137-140`) — **do not import it**. Sprint architecture's
Design Rationale (Step 6) explicitly decided this two-line mapping is
duplicated here rather than imported, because `vision/` is
daemon-only detection machinery and `tags/` must work with no daemon
process anywhere; an import edge from `tags/` to `vision/` would couple
two modules with no shared reason to change together. Add a short
comment in both files cross-referencing the other so a future
dictionary change does not silently desync.

This ticket also executes UC-21's already-recorded packaging decision:
move `opencv-contrib-python>=4.8` from the `daemon` extra to base
`dependencies` in `pyproject.toml`. `zeroconf>=0.131` is **already** in
base `dependencies` — do not touch it; this ticket's only
`pyproject.toml` edit is the `opencv-contrib-python` move.

## Files to touch

- `src/aprilcam/tags/generator.py` (new) — concrete `TagGenerator`.
- `src/aprilcam/errors.py` — add `UnwritableOutputPath(AprilCamError)`,
  the one new error this sprint needs. `InvalidTagId` **already
  exists** (`src/aprilcam/errors.py:135-148`) with exactly the
  signature SUC-002 needs: `__init__(self, family: str, tag_id: int,
  valid: range)`, message `f"{family} id {tag_id} outside valid range
  {valid.start}..{valid.stop - 1}"` — reuse it verbatim, do not
  redefine it.
- `pyproject.toml` — move `opencv-contrib-python>=4.8` (currently line
  ~55, under `[project.optional-dependencies].daemon`) into
  `[project].dependencies` (currently lines 34-44). Leave every other
  entry in both lists untouched, including `zeroconf>=0.131` (already
  base) and the rest of the `daemon` extra (`mss`, `websockets`,
  `fpdf2`, `cv2-enumerate-cameras`, `msgpack`, `grpcio-reflection`).
- `tests/aprilcam/tags/` (new directory — does not exist yet) —
  `test_generator.py` with the unit tests below.

## Acceptance Criteria

- [ ] `TagGenerator.valid_ids(TagFamily.APRILTAG)` returns a `range`
      matching OpenCV's real `DICT_APRILTAG_36h11` dictionary size
      (587 ids, confirmed against the OpenCV 4.11 this sprint was
      planned against — assert the actual dictionary size
      programmatically via `cv2.aruco.getPredefinedDictionary(...)
      .bytesList.shape[0]`, not a hardcoded `587` literal, so the test
      stays correct if the installed OpenCV version changes the
      dictionary).
- [ ] `TagGenerator.valid_ids(TagFamily.ARUCO)` returns a `range`
      matching `DICT_4X4_50` (50 ids), same programmatic-assertion
      approach.
- [ ] SUC-001: `generate(TagFamily.ARUCO, (1,...,10), size_cm=8,
      out_dir=<dir>, dpi=300)` writes ten distinct PNG files, one per
      id; each image's pixel dimensions match `size_cm` at `dpi`
      (within normal rounding, e.g. `round(size_cm / 2.54 * dpi)`);
      nothing else in `out_dir` is touched.
- [ ] SUC-002: requesting an id outside `valid_ids(family)` (e.g. ArUco
      id 55, where `valid_ids` is `range(0, 50)`) raises `InvalidTagId`
      naming the valid range, **and writes nothing for any id in the
      same batch** — including in-range ids requested alongside the
      bad one (e.g. `ids=(1, 2, 3, 55)` must leave `out_dir` empty, not
      partially populated with 1-3). This means every id in a batch is
      validated against `valid_ids` before any image is rendered or
      written — a single up-front validation pass, not a
      validate-then-render loop that could partially succeed.
- [ ] SUC-003: an unwritable `out_dir` (e.g. a read-only directory)
      raises `UnwritableOutputPath` — a typed `AprilCamError` — **before
      any image-encoding call**, verified by a call-count assertion
      (e.g. monkeypatch/mock the encode-and-write step and assert it
      was never called) rather than only checking no file landed on
      disk. `UnwritableOutputPath` must be usable with the existing
      `cli.cli._common.report_error` helper (i.e. it needs nothing
      beyond deriving from `AprilCamError` with a clear `str()`).
- [ ] A requested `size_cm`/`dpi` combination that is unachievable
      (e.g. too small to render legibly) **warns** (e.g.
      `warnings.warn` or a logged WARNING — implementer's call, document
      the choice and threshold rationale in the code as the sprint
      architecture's Open Question 4 anticipates) rather than raising
      or silently producing an unusable image; the image is still
      generated and written.
- [ ] No import of `daemon`, `client`, or `vision` anywhere in
      `tags/generator.py`'s call path (verify by reading the import
      list — no test needed beyond the existing cv2-free-package
      static gate below, since `tags/` is not the `daemon`/`vision`
      side of that boundary).
- [ ] `tests/test_no_cv2_in_client_modules.py` is confirmed unaffected:
      read `_CV2_FREE_PACKAGES = ("client", "view", "mcp")` in that
      file and note in this ticket's completion notes that `tags/` was
      never in scope and adding `cv2` there introduces no violation —
      do not modify that test file.
- [ ] `pyproject.toml`: `opencv-contrib-python>=4.8` appears in
      `[project].dependencies`, not in `[project.optional-dependencies]
      .daemon`. `pip install aprilcam` (no extras) now pulls OpenCV.
      Every other dependency entry (both lists) is unchanged.
- [ ] `uv run mypy` clean on `src/aprilcam/tags/generator.py` and
      `src/aprilcam/errors.py`. Zero `typing.Protocol` anywhere touched.
- [ ] Full existing suite stays green (`uv run pytest`).

## Implementation Plan

**Approach:**
1. Add `UnwritableOutputPath` to `errors.py`, matching `InvalidTagId`'s
   existing pattern (a docstring naming the UC-21 scenario it serves,
   a small `__init__` with a clear message, no message-string parsing
   anywhere else in the codebase).
2. Write `tags/generator.py`:
   - A private `_DICTIONARY_BY_FAMILY` mapping (mirrors, but does not
     import, `vision/detector.py`'s), with a cross-reference comment.
   - `valid_ids(family)` — `range(0, dictionary.bytesList.shape[0])`
     from `cv2.aruco.getPredefinedDictionary(...)`, not a hardcoded
     literal.
   - `generate(family, ids, *, size_cm, out_dir, dpi=300)`:
     1. Validate every id in `ids` against `valid_ids(family)` up
        front; raise `InvalidTagId` for the first offender (naming the
        family, id, and range) before touching the filesystem or
        rendering anything.
     2. Check `out_dir` writability (create it if it does not exist
        and its nearest existing ancestor is writable, matching
        `config/directories.py`'s `_writable_or_creatable` pattern for
        the "creatable" half — or simpler, a direct writability probe;
        implementer's call, but it must run before any rendering) and
        raise `UnwritableOutputPath` if not.
     3. Compute pixel size from `size_cm`/`dpi`
        (`round(size_cm / 2.54 * dpi)`); warn if the result is below
        whatever legibility threshold is chosen (document the
        threshold and its rationale in a code comment — this is
        explicitly left as an implementation judgment call, sprint
        architecture Open Question 4).
     4. Render each id via `cv2.aruco.generateImageMarker(dictionary,
        tag_id, pixel_size)` (the archive's `render_tag` in
        `archive/src/aprilcam/tags/taggen.py` used this same OpenCV
        call as an idea source — do not port its PDF/label/quiet-zone
        code, none of that is in scope) and write one PNG per id to
        `out_dir`, returning the tuple of written `Path`s.
3. Move the `opencv-contrib-python` line in `pyproject.toml`.
4. Write `tests/aprilcam/tags/test_generator.py` covering every
   acceptance criterion above.

**Files to create/modify:**
- `src/aprilcam/tags/generator.py` (new)
- `src/aprilcam/errors.py` (add `UnwritableOutputPath`)
- `pyproject.toml` (dependency move)
- `tests/aprilcam/tags/test_generator.py` (new)
- `tests/aprilcam/tags/__init__.py` if the test tree needs it to match
  this repo's existing `tests/aprilcam/*/` convention (check an
  existing sibling directory, e.g. `tests/aprilcam/client/`, for
  whether `__init__.py` files are used there before deciding)

**Testing plan:**
- Fixture/headless only — no daemon, camera, or network needed
  anywhere in this feature, matching the sprint's Test Strategy.
- Use `tmp_path` for `out_dir` in the success/SUC-001 test.
- Use a real read-only directory (`tmp_path` subdir with mode `0o444`,
  or monkeypatch the writability check — prefer a real filesystem
  permission where the test environment allows it, since that is what
  SUC-003 actually describes) for the `UnwritableOutputPath` test.
- Assert dictionary sizes programmatically against the real installed
  OpenCV, not hardcoded literals (see Acceptance Criteria).
- Run the full suite (`uv run pytest`) to confirm no regression,
  particularly `tests/test_no_cv2_in_client_modules.py`.

**Documentation updates:** none beyond the code-level rationale
comments called out above (dictionary-mapping cross-reference, DPI/size
warning threshold). No `docs/` file is in this ticket's scope.

## Testing

- **Existing tests to run**: `uv run pytest` (full suite — this ticket
  changes a base dependency, so it is worth confirming nothing outside
  `tags/` broke), `tests/test_no_cv2_in_client_modules.py` specifically.
- **New tests to write**: `tests/aprilcam/tags/test_generator.py` —
  dictionary sizes (both families), SUC-001 success path (ten PNGs,
  correct pixel dimensions), SUC-002 (bad id rejects the whole batch,
  nothing written), SUC-003 (unwritable path fails before any encode
  call), and the size/DPI warning case.
- **Verification command**: `uv run pytest && uv run mypy`
