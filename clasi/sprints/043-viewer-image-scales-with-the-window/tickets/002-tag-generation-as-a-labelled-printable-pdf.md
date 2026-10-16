---
id: '002'
title: Tag generation as a labelled printable PDF
status: open
use-cases: [SUC-003]
depends-on: []
github-issue: ''
issue: tags-generate-should-produce-a-single-printable-pdf-like-v1.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Tag generation as a labelled printable PDF

## Description

Reinstate the v1 tag-sheet workflow as `aprilcam tags generate`
(stakeholder decision 2026-10-16; issue
`tags-generate-should-produce-a-single-printable-pdf-like-v1.md`). Default
output is one multi-page PDF: one tag per 59 x 102 mm page, centred with a
small quiet zone, caption under each tag naming family and id
(`ArUco 4x4 ID: 3`, `AprilTag 36h11 ID: 7`). No size parameter; size is
measured after printing. Read `archive/src/aprilcam/tags/taggen.py` for
page geometry, quiet-zone ratio (v1 used 0.04 for PDF) and caption
placement, then write the v2 version against `tags/interfaces.py`; do not
port the module. v1's `ARUCO_POSITIONS` captions are stale and must not
return (no position text at all).

Design: add a PDF-sheet method to `tags.interfaces.TagGenerator` (working
name `generate_sheet(family, ids, *, out_path) -> Path`) and implement it
in `tags/generator.py` (cv2 imported lazily via the existing
`_import_cv`; PDF via `fpdf2`, embedding an in-memory/temp PNG). Keep
`generate(...)` as the `--png` path. Change `cmd_generate` in
`cli/tags.py` to v1's shape: ids spec (`1-8`, `0-3,7`), `-A/--aruco`
(ArUco 4x4 instead of AprilTag 36h11), `-o` output path, `--png`
(per-id images). `--size-cm` leaves the PDF path; keep it only with
`--png`, optional, and pick and document its default. Validation
(`InvalidTagId` naming the valid range, `UnwritableOutputPath`) runs
before anything is written. Move `fpdf2>=2.7` from the `daemon` extra into
base `dependencies` in `pyproject.toml` (and refresh the lockfile if the
project tracks one).

## Acceptance Criteria

- [ ] `aprilcam tags generate 1-8 -A -o tags.pdf` writes one PDF with 8
      pages, each 59 x 102 mm.
- [ ] Each page has the tag centred with a quiet zone and a caption
      `ArUco 4x4 ID: n` (or `AprilTag 36h11 ID: n`) beneath; no position
      text appears anywhere.
- [ ] No size parameter is needed or accepted for the PDF path.
- [ ] `--png` still writes per-id images (with `--size-cm` optional).
- [ ] An out-of-range id errors naming the valid range and writes
      nothing; an unwritable output path fails before generating.
- [ ] `fpdf2` is a declared base dependency in `pyproject.toml`.
- [ ] UC-21 in `docs/design/use-cases/cli.md` and
      `src/aprilcam/tags/DESIGN.md` describe the printable-sheet
      deliverable, close the "PDF output" open question, and drop the
      required `--size-cm` wording; `cli/DESIGN.md` updated if it
      describes the old flags.

## Implementation Plan

- Files to modify: `src/aprilcam/tags/interfaces.py`,
  `src/aprilcam/tags/generator.py`, `src/aprilcam/cli/tags.py`,
  `pyproject.toml`, `docs/design/use-cases/cli.md`,
  `src/aprilcam/tags/DESIGN.md`, `src/aprilcam/cli/DESIGN.md` (if needed).
- Confirm `mcp/tools/mobile_tags.py` and any other `TagGenerator` caller
  are unaffected.
- Do not touch the viewer or stream code; no dependency on ticket 001.

## Testing

- **Existing tests to run**: `tests/aprilcam/tags/test_generator.py`,
  `tests/aprilcam/cli/test_tags_cli.py`.
- **New tests to write** (hardware-free): in `tests/aprilcam/tags/`,
  generate a sheet to `tmp_path` and assert page count == ids, page
  size 59 x 102 mm, caption text present per page and no position words
  (extract text from the PDF), bad id raises `InvalidTagId` and writes
  no file; in `tests/aprilcam/cli/test_tags_cli.py`, extend the
  `_FakeTagGenerator` pattern to cover the ids spec, `-A`, `-o`, `--png`
  routing, and that no `--size-cm` is required. Hardware-marked tests
  (`test_taggen_hardware.py`) stay untouched.
- **Verification command**: `uv run pytest tests/aprilcam/tags tests/aprilcam/cli/test_tags_cli.py`
