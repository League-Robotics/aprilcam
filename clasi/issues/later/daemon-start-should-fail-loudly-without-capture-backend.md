---
status: pending
---

# `daemon start` should fail loudly when no capture backend is available, instead of running camera-less

## Description

Observed twice on 2026-08-23: `aprilcam daemon restart` spawned the
pipx-installed build, whose venv lacks the `[daemon]` extra (no
OpenCV). The daemon started "successfully", printed
"daemon restarted (5 camera(s))" (a registry count, not live devices),
reported `running` in `daemon status`, and enumerated **zero** physical
cameras — every camera `absent`/`unusable` — with no hint that the
process simply has no capture backend. The operator-visible failure
(dead views, absent cameras) is far downstream of the actual problem.

Two layers worth fixing, either or both:

1. **Fail loudly**: if the daemon process cannot import its capture
   backend (cv2), `daemon start` should refuse to start (or start and
   print a prominent warning naming the missing extra and the install
   command), rather than running camera-less. "5 camera(s)" from the
   registry when live enumeration found 0 is exactly the misleading
   shape to avoid.
2. **Start the right build**: `daemon start`/`restart` execs whatever
   `aprilcam` resolves on PATH. On a dev host where the repo venv is
   the real daemon environment, consider having start/restart re-exec
   the daemon with the same interpreter that owns the daemon deps, or
   at least print which interpreter/venv the spawned daemon runs from
   so a mismatch is visible in one line.

Operational workaround documented in
`docs/knowledge/daemon-restart-launches-pipx-build-no-cameras.md`.

## Update 2026-08-26 — recurred; the proposed cv2 check would NOT have caught it

Third occurrence. Same operator-visible shape (all six cameras
`absent`/`unusable`; `camera view 3` hung instead of erroring), but the
missing module was **`cv2-enumerate-cameras`, not `cv2`** — `cv2` was
installed and importable the whole time (opencv-contrib-python 5.0.0.93).

This sharpens requirement 1 above: a readiness check that probes only
`import cv2` would have passed and still enumerated zero cameras. The
daemon's capture path has (at least) two independent import-time
dependencies, and the *enumeration* one is what silently empties the
camera list:

- `camera/identity.py::_enumerate_cv2()` swallows `ImportError` with a
  bare `pass` and returns `{}`.
- `resolve_all()` iterates that empty table → `{}`.
- `discovery.probe()` sees zero hardware keys → every registry camera
  is marked `present=False, usable=False`.

Suggested refinement:

1. Gate on the daemon extra as a **set**, not on `cv2` alone — verify
   every `[daemon]` requirement imports at startup and name the ones
   that don't, plus the install command. On this host `mss`,
   `websockets`, `fpdf2`, `msgpack` and `grpcio-reflection` were also
   missing and had not yet been noticed.
2. Stop `_enumerate_cv2()` swallowing `ImportError` silently — at
   minimum `logger.warning` naming the missing package. A bare `pass`
   here is what makes the whole failure invisible; the operator sees
   only a downstream hang.
3. Consider making a *hang* impossible here regardless: `camera view`
   on a camera the daemon reports `absent` should refuse up front with
   that reason, rather than subscribing to a stream that will never
   yield a frame.

Root cause of the bad install this time was `pipx install <path>`
without the `[daemon]` extra (see the knowledge doc). Fixed on this
host by reinstalling with the extra; the code-level gaps above remain
open.
