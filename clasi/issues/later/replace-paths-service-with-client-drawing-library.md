---
status: pending
---

# Replace paths-as-a-service with a client-side drawing library

Per [specification.md](../../docs/design/specification.md) §4.6 and §6
(stakeholder decision): drawing on the playfield — paths, waypoints,
markers, overlays — is a **Python library, not a service**. There is no
multi-client collaboration on paths, no path storage in the daemon, and
no agent-facing path tools. A program that wants agent-driven drawing
embeds its own MCP server on top of this library.

## Remove

- `src/aprilcam/server/paths.py` (`Waypoint`, `Path`, `PathRegistry`).
- Per-camera `paths.json` persistence and the `paths_file` pointer in
  `info.json`.
- Daemon `GetPaths`/`SetPaths` file-proxy RPCs.
- MCP tools `create_path`, `delete_path`, `list_paths`, `clear_paths`,
  `set_live_overlay`, `clear_live_overlay`, and `_push_paths_rpc`.
- `PublishOverlay` RPC and the `OverlayFrame` message in the proto
  (the tag stream carries only `TagFrame` afterwards).
- Path polling/rendering plumbing in the viewer (`_load_paths`, the
  two-source clear-all logic).

## Add

- A drawing/annotation library in `client/`: fetch a frame (raw or
  deskewed), overlay paths/waypoints/markers/shapes in **world
  coordinates**, using stream metadata for the cm↔px mapping.
  Pure-Python (Pillow), cv2-free. Absorbs the drawing helpers from
  `ui/display.py` (`draw_paths`, `draw_live_overlay`, symbol set,
  RGB-storage/BGR-at-draw-site discipline becomes RGB-only under
  Pillow).
- The viewer renders its local annotations through this library
  ([[viewer-subscribes-to-deskewed-stream]]).

Nothing about drawings transits or persists in the daemon; a program
that wants to keep its paths keeps them itself.

Depends on: [[daemon-publish-deskewed-playfield-stream]] (metadata for
the cm↔px mapping).
