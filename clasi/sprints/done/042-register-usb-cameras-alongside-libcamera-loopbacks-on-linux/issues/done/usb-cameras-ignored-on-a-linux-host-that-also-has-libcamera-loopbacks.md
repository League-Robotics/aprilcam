---
status: done
sprint: '042'
tickets:
- 042-001
- 042-002
- 042-003
---

# USB cameras are ignored on a Linux host that also has libcamera loopback cameras

Found live on vidar.local (Pi 5) on 2026-09-15, after the stakeholder plugged a
USB camera in alongside the working imx296 CSI camera. The stakeholder expects
USB cameras to be the more common setup going forward.

## Evidence

- `lsusb` shows `0c45:636b Microdia Streaming Camera`, and
  `/sys/class/video4linux/video0` has driver `uvcvideo`.
- Running the enumeration layer inside the installed daemon venv (v0.20260914.2,
  after sprint 041) returns
  `_enumerate_cv2() == {0: _EnumEntry(name='Streaming Camera: Streaming Cam', vid=3141, pid=25451, path='/dev/video0'), 70: _EnumEntry(name='aprilcam-imx296-88000', path='/dev/video70')}`.
  That output is correct: sprint 041's filter and dedup work.
- Yet `aprilcam camera probe -d vidar.local` lists only `[1] imx296` and
  `[2] imx296-2`. The USB camera is never registered.

## Root cause

In `src/aprilcam/camera/identity.py`, `resolve_all()`:

```python
if libcamera_entries is None:
    libcamera_entries = _probe_libcamera()
if libcamera_entries:
    return {idx: resolve_identity(idx, libcamera_entries=libcamera_entries)
            for idx in libcamera_entries}
# ... cv2 enumeration path only reached when there are NO libcamera loopbacks
```

As soon as any libcamera loopback exists, the function returns early, so
cv2-enumerated USB/UVC cameras on the same host are never resolved or
registered. `resolve_identity()` probably has the same either/or shape. Check.

## Expected

- On a host with both kinds of camera, every camera is resolved: each libcamera
  loopback gets its `libcamera:` identity, and each USB/UVC camera gets its normal
  identity (vid/pid/serial/location fallback chain).
- A loopback device such as `/dev/video70` also appears in the cv2 enumeration.
  It must resolve **once**, under its libcamera identity, never a second time as
  a generic `loc:`/USB record.
- Registry numbering stays stable. vidar's existing `imx296` (#1) and `imx296-2`
  (#2) keep their numbers, and the USB camera gets the next number.
- The USB camera is capturable over `-d vidar.local`, via `camera image` and
  `camera view`.
- Offline tests cover a mixed host (loopbacks plus UVC), a loopback-only host, and
  a UVC-only host. The mixed-host case gets live verification on vidar.
