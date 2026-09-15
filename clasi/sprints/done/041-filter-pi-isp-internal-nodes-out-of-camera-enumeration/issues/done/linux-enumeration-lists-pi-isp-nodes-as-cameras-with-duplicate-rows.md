---
status: done
sprint: '041'
tickets:
- 041-001
- 041-002
- 041-003
---

# Linux enumeration lists Raspberry Pi ISP/CSI internal nodes as cameras, each twice

Found live on vidar.local (Pi 5, two imx296 CSI cameras) on 2026-09-15. A
kernel upgrade (6.8.0-1060 → 1064-raspi) left the v4l2loopback DKMS module
unbuilt, so `/dev/video70` and `/dev/video71` were never created. See
`docs/knowledge/v4l2loopback-vanishes-after-kernel-upgrade.md`.

In that state, `aprilcam camera probe -d vidar.local` reported:

- the two real cameras `imx296` and `imx296-2` as `absent unusable`, which is
  correct;
- **plus ten more "cameras"**: `rp1-cfe-fe-image0/1`, `rp1-cfe-csi2-ch0/2/3`,
  and `-2` variants for the second CSI port. These are the Pi 5's
  RP1/PiSP front-end and CSI-2 internal V4L2 nodes, not usable cameras.
- **Every row printed twice**, e.g. `[3] rp1-cfe-fe-image1 ... [3]
  rp1-cfe-fe-image1 ...`.

## Why it matters

- When the libcamera bridge is down, the camera list fills with junk, which
  hides the real diagnosis (missing loopback devices).
- Those nodes can also end up registered in the persistent camera registry
  and get numbers, so they may linger after the loopbacks return.
- Probing raw PiSP/CFE nodes with OpenCV could interfere with libcamera, which
  owns them.

## Expected

- Linux enumeration should skip V4L2 nodes that are not standalone capture
  cameras. Examples: the `rp1-cfe-*` and `pispbe-*` media-controller nodes, and
  generally nodes that belong to a media graph whose sensor libcamera owns.
  v1's `archive/src/aprilcam/camera/camutil.py` / `identity.py` may already
  encode a filter.
- The `camera list`/`probe` CLI should print each camera once. Find whether
  the duplication comes from the daemon returning two records or from the CLI
  rendering.
- Any registry entries already created for these nodes on vidar should be
  cleaned up or ignored.
