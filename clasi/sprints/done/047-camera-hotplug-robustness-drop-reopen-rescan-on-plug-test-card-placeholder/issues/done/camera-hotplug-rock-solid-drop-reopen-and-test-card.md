---
status: done
sprint: '047'
tickets:
- 047-001
- 047-002
- 047-003
- 047-004
- 047-005
- 047-006
- 047-009
- 047-010
---

# Camera hotplug must be rock solid: rescan on plug/unplug, drop and reopen devices freely, serve a test-card frame while a camera is gone

## Description

Stakeholder, 2026-11-01, after moving USB cameras between buzzkill and
donald (Linux, `OpenCVBackend`): "I need this to be rock solid. I want
rescans to be done on plug and unplug. ... What does 'in use' mean? ...
You should just be able to drop the camera. If somebody's asking for
frames, you give them [a TV test card], and then you go back to giving
them images after the camera's back."

### Observed (buzzkill, 2026-11-01)

1. After unplugging a camera, `camera list` kept reporting it
   `present usable` indefinitely.
2. After re-plugging a camera (new `/dev/videoN`), `camera image N` and
   `playfield image` failed with `no frame available from camera 'N'`
   until `systemctl restart aprilcamd`.
3. `camera probe` skipped those cameras as "in use by a live
   subscriber", so a rescan could not fix it.

### Why (code map, 2026-11-01)

- **"In use" is not a subscriber count.** `DaemonService._cameras` (the
  held-camera cache, `service.py:66-71`, filled by `_camera_for`
  2853-3018) is treated as "the live-subscriber table", and probe skips
  every slug in it (`camera/discovery.py:238-256`, wired at
  `daemon/__main__.py:114-121`). One `camera image` call puts a camera
  there for the daemon's lifetime; nothing ever removes an entry.
- **A held camera's presence is never re-checked.** `list_cameras`
  reports `held.state` (`service.py:805`), whose `_present` changes only
  on `read()`/self-heal (`camera.py:430-439, 627-701`). The 2 s `watch()`
  poll updates only `_present_slugs` for non-held cameras. With nobody
  reading, an unplugged camera stays present forever.
- **`check_stall` is inert on Linux:** it needs `heartbeat_age()`, which
  only `WorkerBackend` (macOS default) provides.
- **No frame means an error or silence.** `get_frame` raises a bare
  `RuntimeError` (gRPC INTERNAL, logged as "unhandled error"),
  `service.py:943-944, 1018-1019`; tag/image stream producers silently
  skip ticks, so subscribers receive nothing (`streaming.py:476-498,
  856-858`).
- Reopen *does* re-resolve the index through `index_resolver`
  (`opencv_backend.py:239-246`, `worker_backend.py:308-315`), so a
  re-plugged camera on a new node can be found -- it just never gets the
  chance without 5 consecutive failed reads.

### Needed

1. **Device and subscription are separate.** A client subscribes to a
   camera *stream* (by number/slug), never to the device handle. The
   daemon may drop and reopen the device at any time without disturbing
   subscribers. "In use" for probe purposes means a live stream
   subscriber or an in-flight read -- and even then probe must be able
   to verify presence without disturbing it.
2. **Plug/unplug drives rescans automatically.** The hardware watch must
   cover held cameras too: when a held camera's hardware key disappears
   from enumeration, close its device immediately and mark it absent;
   when the key reappears (possibly on a new `/dev/videoN`), reopen it,
   re-apply its stored config (exposure etc. -- today reopen does not
   re-apply config), and resume serving. No restart, no manual probe.
   Consider an event source (udev/netlink on Linux) rather than only the
   2 s poll; the macOS enumeration staleness in
   `cameras-plugged-in-after-daemon-start-are-invisible-until-restart-on-macos.md`
   is the same problem on darwin.
3. **Test-card frames while a camera is gone.** While a camera is absent
   or reopening, `get_frame`, `playfield image` and image streams return
   a generated test card (in the style of the classic RCA Indian-head
   card's geometry: bullseye rings, resolution wedges, gray-scale steps;
   no caricature head) at the camera's configured frame size, labelled
   with the camera's number/name and "NO SIGNAL", and flagged as a
   placeholder in the response metadata so a consumer can tell it from
   a real frame. Tag streams keep sending heartbeats with an explicit
   "camera absent" status instead of going silent. Real frames resume
   automatically when the camera returns.
4. **Honest `camera list`.** Presence for held cameras reflects the
   current enumeration, within a couple of seconds of a plug/unplug.
5. **Close/idle.** Fold in
   `no-way-to-close-a-camera-without-restarting-the-daemon.md`: release
   a device with no subscribers after an idle timeout, and offer an
   explicit close.

### Acceptance

On a Linux UVC host and on macOS: with `camera view N` (or a tag
stream) running, unplug camera N -> within ~2 s `camera list` shows it
absent and the viewer shows the test card; plug it back into any port
-> within ~2 s real frames resume in the same viewer, with the stored
exposure settings applied, no daemon restart and no manual probe.
`camera image N` while unplugged returns the test card (flagged), not
an INTERNAL error.

### Open question to verify on hardware

Whether OpenCV's V4L2 `isOpened()` stays true after unplug and how long
`read()` blocks before failing -- this decides whether enumeration (or
udev) has to be the unplug signal rather than read failures.
