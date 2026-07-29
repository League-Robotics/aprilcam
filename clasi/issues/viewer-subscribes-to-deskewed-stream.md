---
status: pending
---

# Viewer becomes a basic, cv2-free deskewed-stream subscriber

Per [specification.md](../../docs/design/specification.md) §4.8 and §9
(stakeholder decisions): `aprilcam view` is for **basic viewing only** —
subscribe to the deskewed stream and tag stream, render tags and local
annotations, done. Sophisticated visualization belongs in separate
programs built on the client library (which may embed their own MCP
servers for agent-driven drawing).

## Scope

- Subscribe to the daemon's deskewed playfield stream
  ([[daemon-publish-deskewed-playfield-stream]]) instead of warping raw
  frames locally; drop the viewer's cv2/warp dependency entirely.
- Render tags and annotations via the client drawing library
  ([[replace-paths-service-with-client-drawing-library]]).
- Keep the collapsible-panel layer toggles (panel collapse disables the
  corresponding render layer, as today).
- Remove path-file polling and overlay-stream rendering (gone with the
  paths service).
- Extract from `cli/view_cli.py` (1,185 lines) into its own `view/`
  subsystem per the package restructure
  ([[package-restructure-v2-layout]]); `cli/` keeps only the thin
  dispatch shim.
