---
status: in-progress
sprint: '043'
tickets:
- 043-002
---

# Reinstate the v1 tag generator: `tags generate` produces one labelled PDF, no size parameter

## Description

**Stakeholder decision, 2026-10-16:** the v1 tag generator
(`archive/src/aprilcam/tags/taggen.py`) was the right design and v2
must go back to it. Sprint 028's `aprilcam tags generate` (UC-21) is a
regression for the actual printing workflow and is not what the
stakeholder uses.

What v1 did, and what v2 must do again:

- **One multi-page PDF** holding every requested tag, one tag per
  59 x 102 mm label page, centred with a quiet zone. The stakeholder
  prints the whole file at 100% on the label printer. Nothing else.
- **A caption under every tag** naming the family and the id, e.g.
  `ArUco 4x4 ID: 3` / `AprilTag 36h11 ID: 7`. The stakeholder relies on
  this to tell the printed tags apart.
- **No size parameter.** The tag fills the label page; the stakeholder
  prints, measures the result (currently 46 mm for the ArUco set), and
  records that. Size is an output of printing, not an input to
  generation.
- v1's CLI shape: ids spec (`1-8`, `0-3,7`), `-A/--aruco` to choose
  ArUco 4x4 over AprilTag 36h11, `-o` output path, `--png` as an
  opt-in alternative for per-id images.

What v2 did instead, which prompted this issue: one bare PNG per id, no
quiet zone, no caption, no DPI metadata, and a *required* `--size-cm`.
UC-21 listed "PDF output for multi-tag sheets, as v1 had?" as an open
question; sprint 028 answered it by omission. Resolve it the other way.

Workaround used today to reprint main-playfield ArUco 1-8: ran the
archived script directly with `python archive/src/aprilcam/tags/taggen.py
1-8 -A -o ~/Desktop/aruco-tags-1-8.pdf`. cv2, numpy and fpdf2 are all
still in the environment, so it works, but it should not be how tags get
printed.

### Needed

- `aprilcam tags generate` writes the v1-style labelled PDF by default
  and takes v1's arguments. `--size-cm` goes away or becomes optional;
  if kept, it sets the drawn edge length on the page in real units.
- Captions name family and id. v1's hard-coded `ARUCO_POSITIONS`
  table is stale (captions id 1 "Upper Right"; `main-playfield.json`
  puts id 1 at northwest) and must not come back; either caption from
  the playfield definition or omit the position.
- `fpdf2` becomes a declared dependency (pure Python, fine in the base
  extra), or the PDF is produced another way.
- Update UC-21 in `docs/design/use-cases/cli.md` and
  `src/aprilcam/tags/DESIGN.md`: the deliverable is a printable sheet;
  close the "PDF output" open question.
- Per CLAUDE.md, read v1 for what it knew (page geometry, quiet-zone
  ratio, label placement) and write the v2 version against
  `tags/interfaces.py`; do not port the archive module wholesale.
