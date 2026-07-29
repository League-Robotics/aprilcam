---
status: pending
---

# Daemon publishes the deskewed, calibrated playfield stream

Per [specification.md](../../docs/design/specification.md) §4.1 and
principle 2 ("expensive shared processing happens once"), the daemon must
publish the deskewed top-down playfield image as a first-class stream.
Today no deskewed stream exists: every client (viewer, `PlayfieldDisplay`)
warps raw frames itself, duplicating the most expensive per-frame
operation across processes.

## Scope

- New stream type alongside the raw image stream: deskewed frames,
  computed once per frame in `CameraPipeline`, published over the same
  `[uint32 BE length][protobuf]` socket mechanism with on-demand
  endpoint allocation and idle gating (skip the warp entirely when no
  deskew subscriber is live).
- **Sizing rule (stakeholder decision, spec §9.1):** the field rectangle
  is scaled aspect-preserved to fit a bounding box, default **800×800**,
  overridable per playfield. Output is uniform-scale by construction;
  px-per-cm is a *derived, reported* property — never a chosen constant
  (`DEFAULT_PX_PER_CM = 10.0` thinking must not leak into the stream).
- Stream metadata carries frame dimensions and field cm dimensions so
  clients convert deskewed-pixel ↔ world-cm exactly. Frame size is
  stable for the life of a stream.
- Proto extension: deskewed variant in `GetImageStream` (or a new RPC),
  metadata message; regenerate via `scripts/compile_proto.py`.
- Client library: deskewed-stream consumer in `client/`.

## Acceptance sketch

A cv2-free client subscribes, receives fixed-size deskewed JPEG frames
≤800px on the long edge, and converts a known tag's deskewed-pixel
position to world cm using only stream metadata, matching `get_tags`
world output within tolerance.

Related: [[viewer-subscribes-to-deskewed-stream]],
[[replace-paths-service-with-client-drawing-library]].
