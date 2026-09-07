---
status: pending
---

# Uncalibrated tag records should report an image-space yaw, and the CLI should print it in degrees

## Description

Stakeholder request, 2026-09-06. Two new cameras (`streaming-camera`,
`global-shutter-camera`) are used with no playfield and no calibration,
purely to watch tags rotate. `aprilcam camera tags 7` prints:

```
  apriltag 16    UNCALIBRATED -- pixel=(777.6, 764.9)
  apriltag 14    UNCALIBRATED -- pixel=(1007.1, 764.8)
```

with no orientation at all. `daemon/detection.py`'s
`_build_tag_record` deliberately sets `yaw_rad=None` for an
uncalibrated record (sprint 021's "honestly uncalibrated" contract:
only `pixel`/`pixel_corners` populated), and
`cli/camera.py`'s `_format_tag_row` prints yaw only in the calibrated
branch. So there is nothing to print even though the detector has the
four `pixel_corners` and the front edge is fully known.

### Needed

1. **Daemon**: derive an image-space yaw from `pixel_corners` for
   uncalibrated records (the `corners[0]->corners[1]` front edge, same
   edge `_front_edge_yaw` uses in world space). Stakeholder-specified
   convention: **0 = toward the top of the image, degrees**, with a
   documented sign (recommend clockwise positive as seen on screen,
   since image y points down; state it in `types.TagRecord`'s
   docstring). Keep `yaw_rad` as the world-frame field; add a distinct
   field (e.g. `image_yaw_deg`) so the world-first contract in spec
   §4.1 is not muddied -- an uncalibrated record must never look
   calibrated.
2. **CLI**: `_format_tag_row`'s uncalibrated branch prints the image
   yaw in degrees next to the pixel position.
3. **MCP**: `get_tags`/`get_tag`/`get_tag_history` carry the new field
   so an agent gets the same reading.
4. Tests on recorded corner sets covering the four cardinal facings
   and one oblique.

Related: `camera-tags-needs-a-follow-mode-that-streams-detections.md`
(the same stakeholder use case: record rotations over time).
