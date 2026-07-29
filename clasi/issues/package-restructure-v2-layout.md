---
status: pending
---

# Package restructure to the v2 subsystem layout; delete dead code

Per [specification.md](../../docs/design/specification.md) §4 and
[design.md](../../docs/design/design.md) §8: the package directory
layout must match the v2 subsystem decomposition (CLASI's design-doc
set requires one co-located `DESIGN.md` per top-level package
directory, so the layout *is* the design decomposition).

## Moves

- `server/` → `mcp/` (after the web server and paths module are gone it
  contains only the MCP server).
- `core/` dissolves: stateless detection/tracking (`detector.py`,
  tracker) → `vision/`; stateful pipeline + playfield boundary
  (`aprilcam.py`, `playfield.py`) → `daemon/`; `playfield_def.py` →
  a new `playfield/` package (definition schema + handling).
- Viewer out of `cli/view_cli.py` + `ui/` into `view/`; remaining
  drawing helpers from `ui/display.py` into the client drawing library;
  `ui/` is then deleted.
- Each move carries its co-located `DESIGN.md` along and updates the
  root `src/aprilcam/DESIGN.md` and `docs/design/design.md` maps.

## Deletions (dead or superseded)

- `daemon/protocol.py`, `daemon/client.py` — legacy transport, already
  unreferenced (tests assert non-importation).
- `core/pipeline.py`, `core/motion.py` — the duplicate second pipeline
  the daemon never uses.
- Root `stream.py` — self-declared dead code (opens cv2.VideoCapture
  directly).
- `ui/tui.py` — no importers.
- `iohelpers.py` and the legacy `AppConfig` class in `config.py`
  (`core/aprilcam.py` `get_data_dir` usage migrates to `Config`).
- Legacy `homocal` CLI in `calibration/homography.py` and the hardcoded
  upper-left-origin `ARUCO_CORNER_WORLD` table (a coordinate-convention
  foot-gun superseded by playfield-def calibration).
- Decide `tags/arucogen.py`: wire into the dispatcher or delete (it has
  a `main()` but no CLI entry).

Sequencing: this restructure should land **after** the functional
issues ([[remove-web-server]],
[[replace-paths-service-with-client-drawing-library]],
[[slim-mcp-server-to-pure-daemon-client]],
[[move-calibration-execution-into-daemon]]) so moves don't churn code
that is about to be deleted.
