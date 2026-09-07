---
status: pending
---

# Stream subscribe errors are dropped by the transport, so `playfield view` says "no frame received" instead of "not calibrated"

## Description

2026-09-06: `aprilcam playfield view 7` on an uncalibrated camera
printed the FirstFrameTimeout diagnostic ("the camera's capture session
may be wedged or its worker down ... restart the daemon") while the
daemon log showed the real cause:

```
stream_transport(images-deskewed-7): subscribe failed -- closing connection
aprilcam.errors.NotCalibrated: '7' is not calibrated
```

`daemon/stream_transport.py`'s `_sender_loop` catches the subscribe
exception, logs it, and closes the socket without sending anything.
The client sees a silent close, times out, and `cli/playfield.py`'s
`NotCalibrated` handler (the UC-18.S-2 "calibrate first" pointer) is
never reached. The stakeholder was sent chasing a wedged camera that
was fine.

### Needed

- The stream transport sends a typed error frame on subscribe failure
  and the client raises the corresponding `AprilCamError`
  (`NotCalibrated`, `UnknownCamera`, `CameraNotPresent`) so the
  existing CLI handlers fire.
