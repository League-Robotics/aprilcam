---
status: pending
---

# World-coordinates-first: audit the pipeline and make the tag contract world-primary

Stakeholder directive (2026-07-29), now specification principles 5 and 6:
convert to world coordinates as early as possible; track, store, and
exchange world coordinates everywhere; pixels exist only inside daemon
detection internals and at the client's final render step. The current
program has taken shortcuts on this ("disastrous"), and the geometry
conventions (up/east/sign) were hard-won and must not regress.

## Scope

- **Tag contract:** `TagRecord` becomes world-primary — world position
  (cm), world velocity, speed, heading are the contract; `center_px`,
  `corners_px`, `vel_px`, `speed_px` become diagnostic-only fields (and
  the only representation from an uncalibrated camera, explicitly
  flagged). MCP `get_tags`/history and the tag stream present world
  coordinates first; clients plotting on an image convert world →
  pixels via stream metadata.
- **Pipeline audit:** find every place a position is stored, cached,
  compared, or exchanged in pixel space past the detection step, and
  convert it to world space. Known legitimate pixel survivors:
  calibration anchors (`corner_pixels`, `static_markers` — they define
  the mapping), the tag-publish change threshold (`change_threshold_px`
  — decide whether it becomes world-cm), detection internals.
- **Shortcut hunt:** find and eliminate paths that convert
  pixel→pixel between images without going through world coordinates
  (the stakeholder reports such shortcuts exist and cause disasters).
  Rule: to plot anything on any image, go to world first, then world →
  target-image pixels via that image's mapping.
- **Geometry regression tests:** add tests that pin the conventions —
  A1-centred origin, +x east, +y north, north-up on the deskewed image,
  single y-flip in the world→pixel mapping, heading 0 = east CCW —
  so no future change can silently mirror an axis. These invariants are
  listed in [design.md](../../docs/design/design.md) §6.

Related: [[daemon-publish-deskewed-playfield-stream]] (the metadata that
makes client-side conversion exact),
[[replace-paths-service-with-client-drawing-library]] (the render-step
conversion site).
