# src/aprilcam/

**Owner:** Eric Busboom · **Last reviewed:** 2026-08-21 · **Status:** stable

---

## 1. Purpose

This is the sole declared source root for AprilCam (`.clasi/config.yaml`'s
`sources: [src/aprilcam]`). It holds the whole `aprilcam` Python package: the
perception daemon, its client library, the MCP server, the CLI, the basic
viewer, and the offline fiducial-generation utility, plus the small set of
top-level modules every subsystem shares. This document is a map of the tree
— what lives here and how the pieces are meant to relate — not a restatement
of subsystem internals (each subsystem's own co-located `DESIGN.md` owns
that) and not a restatement of system-wide architecture (`docs/design/
design.md` owns that; read it first for the principles below).

## 2. Orientation

Thirteen subsystem directories, each with its own `DESIGN.md`:

| Subsystem | One line |
|---|---|
| `daemon/` | The `aprilcamd` process: sole camera owner, sole vision authority, gRPC control plane, stream publishing, config/mobile-tag/annotation services. |
| `camera/` | Camera identity resolution across re-plugs, the persistent identity→slug registry, and pluggable capture backends. Daemon-only. |
| `vision/` | Stateless frame vision: preprocessing pipeline and raw AprilTag/ArUco detection — pixels in, raw hits out. Daemon-only. |
| `calibration/` | Establishes and holds a camera's pixel↔world mapping from a playfield definition; deskew geometry. Daemon-only. |
| `intrinsics/` | ChArUco-board lens (intrinsics) calibration as a daemon workflow: detector, view collector, solver, sessions. Daemon-only except its cv2-free `interfaces.py`. |
| `playfield/` | The surveyed playfield as a definition document plus queries against it (not a camera, not a service). |
| `config/` | FHS/XDG directory resolution and the config-document value types/loader (camera configs, playfield definitions). |
| `client/` | The one sanctioned way to talk to the daemon: gRPC wrapper, discovery, stream consumers, `from_proto` model boundary, annotation renderer. cv2-free. |
| `mcp/` | The FastMCP stdio server exposing perception and configuration as agent tools. Pure daemon client. |
| `view/` | The basic Tk live-view window: subscribes to a daemon stream and renders it. Deliberately not a full UI. |
| `cli/` | The `aprilcam` dispatcher and one thin module per subcommand, all daemon clients. |
| `tags/` | Offline AprilTag/ArUco fiducial image generation — the one capability that needs no daemon. |
| `proto/` | Generated protobuf/gRPC wire contract, regenerated from `.proto` sources, never hand-edited. |

## 3. Conventions every subsystem doc may assume

These are established at the system level (`docs/design/design.md` §2 and
§6) and are not repeated in each subsystem doc — see that document for the
authoritative statement. In summary, so a reader of any one subsystem doc
has the vocabulary without a second lookup:

- **Single vision authority.** All OpenCV work — capture, detection,
  tracking, homography, deskew, calibration — runs inside `daemon/` only.
  The lone sanctioned exception is `tags/`, an offline utility with no
  camera or daemon dependency.
- **World coordinates first; pixels only at the edges.** Positions are
  never stored in pixels; pixel coordinates exist only inside the
  daemon's detection internals and at a client's final render step.
- **Geometry conventions are sacrosanct**: A1-centred world origin, +x
  east, +y north; north is up on a deskewed image; heading 0 = east, CCW
  positive. No change touching geometry lands without demonstrating these
  still hold.
- **Whole-document configuration.** Playfield definitions and camera
  configs move as complete documents; there are no field-level edit RPCs.
  Runtime registrations (mobile-tag parameters, annotations) are
  record-level and are not configuration.

Four top-level modules, not owned by any one subsystem because every
subsystem needs them, live directly under this root:

- **`types.py`** — the shared value vocabulary (`TagId`, `PixelPoint`,
  `WorldPoint`, `TagFamily`, …) that every subsystem's `interfaces.py` is
  defined in terms of. Encodes the world-frame and heading conventions
  above as executable types, not just prose. **Sprint 039** adds
  `TagRecord.image_yaw_deg`, a diagnostic-only image-pixel-space field
  distinct from the world-frame `yaw_rad` above — see `docs/design/
  design.md` §6 for the sign convention and why it exists as a second,
  separate field rather than an alternate reading of `yaw_rad`.
- **`errors.py`** — the client-visible error taxonomy. Every distinction a
  use case draws about failure is a type here, not a message string, so an
  adapter (CLI, MCP) can dispatch on the exception type rather than
  parsing text.
- **`framing.py`** — the `[uint32 BE length][payload]` wire framing shared
  by every bulk-stream socket. Deliberately sits here rather than under
  `daemon/` or `client/`: both need it and neither imports the other.
- **`guides.py`** — packaged agent/robot guide text, read identically by
  `client.guides.Guides` and the daemon's `GetGuide` RPC, so the same
  guide text is available with or without a running daemon.

No subsystem doc should re-describe these four modules' own contracts in
detail; reference this section instead.

