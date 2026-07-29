# aprilcam — source tree overview

**Owner:** Eric Busboom · **Last reviewed:** 2026-07-29 · **Status:** in-flux (v1 → v2 transition)

This is the root overview of the `aprilcam` package. System-wide context,
the subsystem map, and global conventions live in
[docs/design/design.md](../../docs/design/design.md); each subsystem's
own design lives in its co-located `DESIGN.md`. This file covers only
what belongs to the tree as a whole.

---

## Package layout

| Package | What it is | Details |
|---|---|---|
| `daemon/` | The aprilcamd process — camera owner, vision authority, config service | [DESIGN.md](daemon/DESIGN.md) |
| `camera/` | Camera identity, registry, capture backends (daemon-only) | [DESIGN.md](camera/DESIGN.md) |
| `core/` | v1 pipeline + playfield boundary (dissolving into `vision/` + `daemon/` in v2) | [DESIGN.md](core/DESIGN.md) |
| `vision/` | Stateless frame vision: detection, objects, color (daemon-only) | [DESIGN.md](vision/DESIGN.md) |
| `calibration/` | Pixel→world calibration + deskew geometry | [DESIGN.md](calibration/DESIGN.md) |
| `client/` | Thin cv2-free client library: discovery, gRPC, streams, models | [DESIGN.md](client/DESIGN.md) |
| `server/` | MCP server (+ web server, removed in v2); becomes `mcp/` | [DESIGN.md](server/DESIGN.md) |
| `cli/` | `aprilcam` dispatcher + one thin module per subcommand | [DESIGN.md](cli/DESIGN.md) |
| `ui/` | Display/drawing helpers + viewer internals (folding into `client/` + `view/` in v2) | [DESIGN.md](ui/DESIGN.md) |
| `tags/` | AprilTag/ArUco generation (offline utility) | [DESIGN.md](tags/DESIGN.md) |
| `proto/` | Generated protobuf modules — never hand-edited | [DESIGN.md](proto/DESIGN.md) |

Top-level modules: `config.py` (directory resolution FHS/XDG, env
cascade, `CONFIG_VARS`), `errors.py`, `guides.py` (packaged
`AGENT_GUIDE.md` / `ROBOT_API_GUIDE.md` readers), `iohelpers.py` +
legacy `AppConfig` in `config.py` (deprecated, v2 removes), root
`stream.py` (dead, v2 removes), `__main__.py` (3-line CLI shim).

## Tree-wide rules

- **The cv2 import boundary is the load-bearing wall.** Only `daemon/`,
  `camera/`, `core/`, `vision/`, `calibration/`, and `tags/` may import
  OpenCV, and only lazily where a cv2-free client could transitively
  import the module. The base install (MCP server, client library,
  viewer, CLI) must work with Pillow/NumPy only — `pipx install
  aprilcam` on a machine with no OpenCV must succeed and every
  non-daemon command must run. Breaking this breaks every thin-client
  deployment.
- **Lazy subcommand imports in `cli/`** exist to keep base-install
  startup working and fast; a top-level import of a daemon-only module
  from a client path is a regression even if tests on a dev machine
  (which has cv2) stay green.
- **Version discipline:** bump `pyproject.toml` (format `0.YYYYMMDD.N`)
  after every substantive change; installs are editable, the version is
  how sessions tell which code is live.
- **Generated code:** `proto/` is produced by
  `scripts/compile_proto.py`; hand edits are lost on regeneration.
